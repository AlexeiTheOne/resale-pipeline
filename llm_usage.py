"""What each model call cost, and the monthly Anthropic budget guard.

Every Claude call (claude_llm.ask) and every Gemini call (llm.generate_with_retry)
writes a row to db.llm_usage. The owner's Anthropic credit is $100 a month, so
the guard reads month-to-date Anthropic spend from that table and:
  - at ANTHROPIC_BUDGET_WARN_PCT, budget_warning() hands the bot ONE message to
    send (once per month, remembered in job_runs so a restart doesn't repeat it);
  - at ANTHROPIC_BUDGET_DOWNGRADE_PCT, choose_model() steps every call down one
    tier (opus -> sonnet -> haiku), and at 100% runs everything on haiku.

Logging never raises into the caller: a failed insert costs us a row of
bookkeeping, not the listing the call was for.
"""
import re
from datetime import datetime, timezone

import config
import db

PER_M = 1_000_000


# --- cost -------------------------------------------------------------------

def _anthropic_rates(model: str) -> dict:
    rates = config.ANTHROPIC_PRICES.get(model)
    if rates is not None:
        return rates
    # Unknown model (a new fallback target, a typo'd override): cost it at the
    # dearest row so the guard over-counts rather than under-counts.
    return max(config.ANTHROPIC_PRICES.values(), key=lambda r: r["out"])


def anthropic_cost(model: str, *, input_tokens: int = 0, output_tokens: int = 0,
                   cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                   web_searches: int = 0) -> float:
    """USD for one Claude attempt. input_tokens is the UNCACHED input, as the API
    reports it; cache reads and writes are separate counts with their own rates."""
    r = _anthropic_rates(model)
    rate_in, rate_out = r["in"], r["out"]
    rate_cr, rate_cw = r["cache_read"], r["cache_write"]
    threshold = r.get("long_threshold")
    if threshold and input_tokens + cache_read_tokens + cache_write_tokens > threshold:
        scale = r["long_in"] / r["in"]
        rate_in, rate_out = r["long_in"], r["long_out"]
        rate_cr, rate_cw = rate_cr * scale, rate_cw * scale
    return (input_tokens * rate_in + output_tokens * rate_out
            + cache_read_tokens * rate_cr + cache_write_tokens * rate_cw) / PER_M \
        + web_searches * config.ANTHROPIC_WEB_SEARCH_PER_1K / 1000


def gemini_rates(model: str) -> dict | None:
    name = (model or "").removeprefix("models/")
    for pattern, rates in config.GEMINI_PRICES:
        if re.match(pattern, name):
            return rates
    return None


def gemini_cost(model: str, *, input_tokens: int = 0, output_tokens: int = 0) -> float | None:
    """APPROXIMATE USD for one Gemini call; None when the model has no price on file.
    output_tokens must already include thinking tokens."""
    rates = gemini_rates(model)
    if rates is None:
        return None
    return (input_tokens * rates["in"] + output_tokens * rates["out"]) / PER_M


# --- logging ----------------------------------------------------------------

def _n(value) -> int:
    return int(value or 0)


def log_anthropic(response, *, stage: str | None, item_id: str | None = None) -> float:
    """Log a Claude response's usage; returns the cost logged. With a refusal
    fallback, usage.iterations holds one entry per model that ran (each billed
    at its own rates) and top-level usage only the last, so iterations win."""
    total = 0.0
    try:
        usage = response.usage
        stu = getattr(usage, "server_tool_use", None)
        searches = _n(getattr(stu, "web_search_requests", 0)) if stu else 0
        iterations = getattr(usage, "iterations", None) or []
        attempts = [it for it in iterations if getattr(it, "model", None)] or [usage]
        for i, att in enumerate(attempts):
            model = getattr(att, "model", None) or response.model
            row = dict(
                input_tokens=_n(att.input_tokens),
                output_tokens=_n(att.output_tokens),
                cache_read_tokens=_n(getattr(att, "cache_read_input_tokens", 0)),
                cache_write_tokens=_n(getattr(att, "cache_creation_input_tokens", 0)),
                web_searches=searches if i == 0 else 0,
            )
            cost = anthropic_cost(model, **row)
            total += cost
            db.record_llm_usage(provider="anthropic", model=model, stage=stage,
                                item_id=item_id, cost_usd=cost, **row)
    except Exception as e:  # bookkeeping must never fail the call
        print(f"WARNING: could not log Claude usage ({stage}): {type(e).__name__}: {e}")
    return total


def log_gemini(response, *, model: str, stage: str | None = None,
               item_id: str | None = None) -> None:
    """Log a Gemini response's usage_metadata (approximate cost)."""
    try:
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return
        input_tokens = _n(um.prompt_token_count) + _n(getattr(um, "tool_use_prompt_token_count", 0))
        output_tokens = _n(um.candidates_token_count) + _n(getattr(um, "thoughts_token_count", 0))
        searches = 0
        for cand in getattr(response, "candidates", None) or []:
            gm = getattr(cand, "grounding_metadata", None)
            searches += len(getattr(gm, "web_search_queries", None) or []) if gm else 0
        db.record_llm_usage(
            provider="gemini", model=model, stage=stage, item_id=item_id,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=_n(getattr(um, "cached_content_token_count", 0)),
            web_searches=searches,
            cost_usd=gemini_cost(model, input_tokens=input_tokens, output_tokens=output_tokens),
        )
    except Exception as e:
        print(f"WARNING: could not log Gemini usage ({stage}): {type(e).__name__}: {e}")


# --- monthly guard ----------------------------------------------------------

def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def anthropic_month_spend(now: datetime | None = None) -> float:
    return db.llm_spend_since(month_start(now).isoformat(), provider="anthropic")


def budget_fraction(now: datetime | None = None) -> float:
    budget = config.ANTHROPIC_MONTHLY_BUDGET
    return anthropic_month_spend(now) / budget if budget > 0 else 0.0


def _tiers() -> list[str]:
    return [config.CLAUDE_STRONG_MODEL, config.CLAUDE_MODEL, config.CLAUDE_CHEAP_MODEL]


def _tier_of(model: str) -> int | None:
    tiers = _tiers()
    if model in tiers:
        return tiers.index(model)
    for i, family in enumerate(("opus", "sonnet", "haiku")):
        if family in model:
            return i
    return None


def choose_model(preferred: str | None = None, *, fraction: float | None = None) -> str:
    """The model to actually call, given what the caller wanted and how much of
    the month's budget is gone. Below the downgrade line it's `preferred`
    unchanged; from there to 100% one tier cheaper; past 100% the cheapest."""
    preferred = preferred or config.CLAUDE_MODEL
    if fraction is None:
        fraction = budget_fraction()
    if fraction < config.ANTHROPIC_BUDGET_DOWNGRADE_PCT:
        return preferred
    tiers = _tiers()
    steps = 1 if fraction < 1.0 else len(tiers)
    tier = _tier_of(preferred)
    if tier is None:  # not a model we know how to rank: go straight to the cheap one
        return tiers[-1]
    chosen = tiers[min(tier + steps, len(tiers) - 1)]
    if chosen != preferred:
        print(f"Anthropic budget {fraction:.0%} used: {preferred} -> {chosen}")
    return chosen


def budget_warning(now: datetime | None = None) -> str | None:
    """A message for the owner the FIRST time this month spend crosses the warn
    line, and again the first time it crosses the downgrade line; None
    otherwise. Remembered in job_runs, so it survives restarts."""
    now = now or datetime.now(timezone.utc)
    spent = anthropic_month_spend(now)
    budget = config.ANTHROPIC_MONTHLY_BUDGET
    if budget <= 0:
        return None
    fraction = spent / budget
    month = month_start(now).strftime("%Y-%m")
    if fraction >= config.ANTHROPIC_BUDGET_DOWNGRADE_PCT:
        level, text = "downgrade", (
            f"🛑 Claude spend this month is ${spent:.2f} of ${budget:.0f} ({fraction:.0%}). "
            "Calls now run one model cheaper (opus→sonnet→haiku), and all on haiku past 100%. "
            "Resets on the 1st. /usage for the breakdown.")
    elif fraction >= config.ANTHROPIC_BUDGET_WARN_PCT:
        level, text = "warn", (
            f"⚠️ Claude spend this month is ${spent:.2f} of ${budget:.0f} ({fraction:.0%}). "
            f"At {config.ANTHROPIC_BUDGET_DOWNGRADE_PCT:.0%} calls drop to cheaper models. "
            "/usage for the breakdown.")
    else:
        return None
    key = f"anthropic_budget_{level}_{month}"
    if db.last_job_run(key) is not None:
        return None
    db.record_job_run(key, detail=f"${spent:.2f}")
    return text


def usage_report(now: datetime | None = None) -> list[str]:
    """Month-to-date spend by provider / model / stage, for /usage."""
    now = now or datetime.now(timezone.utc)
    start = month_start(now)
    rows = db.llm_usage_breakdown(start.isoformat())
    spent = anthropic_month_spend(now)
    budget = config.ANTHROPIC_MONTHLY_BUDGET
    lines = [f"💸 LLM spend since {start:%d %b %Y} (UTC)", ""]
    pct = f" ({spent / budget:.0%})" if budget > 0 else ""
    lines.append(f"Anthropic: ${spent:.2f} of ${budget:.0f} budget{pct}")
    if not rows:
        lines.append("No model calls logged this month.")
        return lines
    by_provider: dict[str, float] = {}
    for r in rows:
        by_provider[r["provider"]] = by_provider.get(r["provider"], 0.0) + (r["cost_usd"] or 0.0)
    for provider, cost in sorted(by_provider.items()):
        note = " (approx.)" if provider == "gemini" else ""
        lines.append(f"{provider}: ${cost:.2f}{note}")
    lines.append("")
    for r in rows:
        cost = "unpriced" if r["cost_usd"] is None else f"${r['cost_usd']:.3f}"
        extra = f", {r['web_searches']} searches" if r["web_searches"] else ""
        lines.append(f"{r['model']} · {r['stage']}: {cost}, {r['calls']} call(s), "
                     f"{r['input_tokens'] or 0:,} in / {r['output_tokens'] or 0:,} out{extra}")
    return lines
