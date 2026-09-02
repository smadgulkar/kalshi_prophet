"""
Claude-as-forecaster: one structured JSON probability estimate per eligible
Kalshi market, following the same prediction context the paper's live
deployment used (Section D.7.1) — title, resolution rules, contemporaneous
market data, web-grounded, single forecast call per market per cycle.
"""

import json
import logging
import os
import re

import anthropic

from config import (
    FORECASTER_EFFORT, FORECASTER_MAX_TOKENS, FORECASTER_MODEL,
    PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK, WEB_SEARCH_MAX_USES,
)

log = logging.getLogger("prophet.forecaster")

_SYSTEM_PROMPT = """You are a calibrated forecaster estimating the probability that a Kalshi \
prediction-market contract resolves YES. You will be given the market's title, its exact \
resolution rules, and the current market-implied price. Use web search to find current, \
relevant information before answering — do not rely on stale training knowledge for anything \
time-sensitive.

Be honest about uncertainty. The market price is itself informed and often close to correct; \
only deviate from it when you have a concrete, articulable reason (specific facts the market \
may be underweighting, a genuine analytical edge), not vibes. Overconfidence is penalized \
identically to underconfidence — this is measured by the Brier score, which is minimized by \
reporting your true belief.

If the resolution rule is ambiguous, discretionary, or does not clearly specify a verifiable \
source of truth (e.g. it depends on an undefined leaderboard, an unspecified operator judgment \
call, or conflicting plausible interpretations), set resolution_ambiguous to true and explain why \
— do not attempt to force a probability estimate onto an unresolvable question.

Respond with ONLY a single JSON object (no markdown fences, no other text) with exactly these \
fields:
{"probability_yes": <float 0.0-1.0>, "rationale": "<one or two sentences, under 300 characters>", \
"resolution_ambiguous": <true|false>, "ambiguity_reason": "<string, empty if not ambiguous>"}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. This shadow-tester calls the Claude API "
                "directly (metered, per-token billing) — see README for setup."
            )
        _client = anthropic.Anthropic()
    return _client


class ForecastError(Exception):
    pass


def _build_user_prompt(market) -> str:
    return (
        f"MARKET TITLE: {market.title}\n"
        f"TICKER: {market.ticker}\n"
        f"RESOLUTION RULES: {market.rules_primary}\n"
        f"ADDITIONAL RULES: {market.rules_secondary or '(none)'}\n"
        f"CLOSES: {market.close_time} ({market.hours_to_close:.1f} hours from now)\n"
        f"CURRENT MARKET PRICE: yes_bid=${market.yes_bid:.2f} yes_ask=${market.yes_ask:.2f} "
        f"(the market currently implies roughly a {(market.yes_bid + market.yes_ask) / 2:.0%} "
        f"chance of YES)\n\n"
        "What is your calibrated probability that this resolves YES?"
    )


def _extract_json(text: str) -> dict:
    m = _JSON_RE.search(text)
    if not m:
        raise ForecastError(f"no JSON object found in response: {text[:200]!r}")
    return json.loads(m.group(0))


def _cost_usd(usage) -> float:
    input_tok = usage.input_tokens or 0
    output_tok = usage.output_tokens or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    return (
        input_tok * PRICE_INPUT_PER_MTOK
        + cache_write * PRICE_INPUT_PER_MTOK * 1.25
        + cache_read * PRICE_INPUT_PER_MTOK * 0.1
        + output_tok * PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000


class Forecast:
    def __init__(self, p_yes, rationale, resolution_ambiguous, ambiguity_reason,
                 input_tokens, output_tokens, cost_usd):
        self.p_yes = p_yes
        self.rationale = rationale
        self.resolution_ambiguous = resolution_ambiguous
        self.ambiguity_reason = ambiguity_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd


def forecast_market(market) -> Forecast:
    client = _get_client()
    messages = [{"role": "user", "content": _build_user_prompt(market)}]
    total_cost = 0.0
    total_in = total_out = 0

    for attempt in range(2):  # one retry on malformed JSON
        response = client.messages.create(
            model=FORECASTER_MODEL,
            max_tokens=FORECASTER_MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": FORECASTER_EFFORT},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}],
            messages=messages,
        )
        total_cost += _cost_usd(response.usage)
        total_in += response.usage.input_tokens or 0
        total_out += response.usage.output_tokens or 0

        if response.stop_reason == "refusal":
            raise ForecastError(f"model refused: {getattr(response.stop_details, 'category', '?')}")

        text_blocks = [b.text for b in response.content if b.type == "text"]
        final_text = text_blocks[-1] if text_blocks else ""

        try:
            parsed = _extract_json(final_text)
            p = float(parsed["probability_yes"])
            if not (0.0 <= p <= 1.0):
                raise ValueError(f"probability_yes out of range: {p}")
            return Forecast(
                p_yes=p,
                rationale=str(parsed.get("rationale", ""))[:500],
                resolution_ambiguous=bool(parsed.get("resolution_ambiguous", False)),
                ambiguity_reason=str(parsed.get("ambiguity_reason", ""))[:500],
                input_tokens=total_in,
                output_tokens=total_out,
                cost_usd=total_cost,
            )
        except (ForecastError, ValueError, KeyError, json.JSONDecodeError) as e:
            log.warning("%s: parse failure attempt %d: %s", market.ticker, attempt + 1, e)
            if attempt == 0:
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": "That was not a valid JSON object matching the schema. "
                               "Reply with ONLY the JSON object, nothing else.",
                })
                continue
            raise ForecastError(f"gave up after 2 attempts: {e}") from e

    raise ForecastError("unreachable")
