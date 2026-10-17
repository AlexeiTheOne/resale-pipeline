"""Spend logging, cost maths and the monthly Anthropic budget guard.

The owner has $100 a month of Anthropic credit. What matters here: a call's cost
is computed from the right rates (cache reads, Haiku's long-prompt tier, web
searches, a refusal fallback billed at the fallback model's own rates); the guard
steps opus -> sonnet -> haiku at the right thresholds; the warning fires once per
threshold per month and not on every restart; and claude_llm.ask never hands back
a refusal or a truncated answer as if it were complete.

No network: the Anthropic and Gemini clients are stubs, and the DB is pointed at
a temp file before anything writes, so the real data/ross.db is never touched.

Run with `python -m pytest tests/test_llm_usage.py`.
"""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_TMPDIR = tempfile.mkdtemp(prefix="llmusage_")
import db  # noqa: E402

db.DB_PATH = str(Path(_TMPDIR) / "test.db")
db.create_tables()

import claude_llm  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402
import llm_usage  # noqa: E402

OCT = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
NOV = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    with db._conn() as con:
        con.execute("DELETE FROM llm_usage")
        con.execute("DELETE FROM job_runs")
    monkeypatch.setattr(config, "ANTHROPIC_MONTHLY_BUDGET", 100.0)
    monkeypatch.setattr(config, "ANTHROPIC_BUDGET_WARN_PCT", 0.80)
    monkeypatch.setattr(config, "ANTHROPIC_BUDGET_DOWNGRADE_PCT", 0.95)
    monkeypatch.setattr(config, "CLAUDE_REFUSAL_FALLBACK", True)


def _spend(usd, when=OCT, provider="anthropic"):
    db.record_llm_usage(provider=provider, model="claude-sonnet-5-5", stage="seed",
                        cost_usd=usd, ts=when.isoformat())


def _rows():
    with db._conn() as con:
        cur = con.execute("SELECT * FROM llm_usage ORDER BY id")
        keys = [d[0] for d in cur.description]
        return [dict(zip(keys, r)) for r in cur.fetchall()]


# --- cost --------------------------------------------------------------------

def test_anthropic_cost_basic_rates():
    assert llm_usage.anthropic_cost("claude-sonnet-5-5", input_tokens=1_000_000,
                                    output_tokens=1_000_000) == pytest.approx(12.0)
    assert llm_usage.anthropic_cost("claude-opus-5-5", input_tokens=1_000_000,
                                    output_tokens=1_000_000) == pytest.approx(24.0)
    assert llm_usage.anthropic_cost("claude-haiku-5-5", input_tokens=10_000,
                                    output_tokens=1_000) == pytest.approx(0.0015)


def test_anthropic_cost_cache_and_web_search():
    # 1M cache-read on opus is $0.20, not $4; 5 searches are $0.05.
    cost = llm_usage.anthropic_cost("claude-opus-5-5", cache_read_tokens=1_000_000,
                                    cache_write_tokens=100_000, web_searches=5)
    assert cost == pytest.approx(0.20 + 0.50 + 0.05)


def test_haiku_long_prompt_tier():
    short = llm_usage.anthropic_cost("claude-haiku-5-5", input_tokens=100_000, output_tokens=1000)
    long = llm_usage.anthropic_cost("claude-haiku-5-5", input_tokens=100_001, output_tokens=1000)
    assert short == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


def test_unknown_claude_model_costed_at_dearest_rate():
    unknown = llm_usage.anthropic_cost("claude-mystery-9", input_tokens=1_000_000)
    assert unknown == pytest.approx(5.0)  # opus-5 / 4.8 input rate, the highest on file


def test_gemini_cost_table():
    assert llm_usage.gemini_cost("gemini-2.5-flash", input_tokens=1_000_000,
                                 output_tokens=1_000_000) == pytest.approx(2.80)
    for model in ("gemini-3.5-flash", "models/gemini-3.8-flash", "gemini-3-flash"):
        assert llm_usage.gemini_cost(model, input_tokens=1_000_000) == pytest.approx(0.75)
    assert llm_usage.gemini_cost("gemini-2.5-flash-lite", input_tokens=10) is None
    assert llm_usage.gemini_cost("gemini-2.5-pro", input_tokens=10) is None


# --- guard -------------------------------------------------------------------

@pytest.mark.parametrize("fraction,preferred,expected", [
    (0.50, "claude-opus-5-5", "claude-opus-5-5"),
    (0.94, "claude-opus-5-5", "claude-opus-5-5"),
    (0.95, "claude-opus-5-5", "claude-sonnet-5-5"),
    (0.95, "claude-sonnet-5-5", "claude-haiku-5-5"),
    (0.99, "claude-haiku-5-5", "claude-haiku-5-5"),
    (1.00, "claude-opus-5-5", "claude-haiku-5-5"),
    (1.40, "claude-sonnet-5-5", "claude-haiku-5-5"),
])
def test_choose_model_thresholds(fraction, preferred, expected):
    assert llm_usage.choose_model(preferred, fraction=fraction) == expected


def test_choose_model_reads_month_to_date_anthropic_spend_only():
    _spend(60.0)
    _spend(40.0, provider="gemini")                            # not on the Anthropic budget
    _spend(90.0, when=datetime(2026, 9, 30, 23, tzinfo=timezone.utc))  # last month
    assert llm_usage.anthropic_month_spend(OCT) == pytest.approx(60.0)
    _spend(36.0)
    assert llm_usage.budget_fraction(OCT) == pytest.approx(0.96)


def test_budget_warning_once_per_threshold_per_month():
    _spend(50.0)
    assert llm_usage.budget_warning(OCT) is None
    _spend(31.0)                                        # 81%
    first = llm_usage.budget_warning(OCT)
    assert first and "81%" in first
    assert llm_usage.budget_warning(OCT) is None        # not repeated (e.g. after restart)
    _spend(15.0)                                        # 96%
    assert "cheaper" in llm_usage.budget_warning(OCT)
    assert llm_usage.budget_warning(OCT) is None
    # A new month starts clean: no spend yet, so nothing to say.
    assert llm_usage.budget_warning(NOV) is None
    _spend(85.0, when=NOV)
    assert llm_usage.budget_warning(NOV) is not None


# --- logging -----------------------------------------------------------------

def _usage(**kw):
    base = dict(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                cache_creation_input_tokens=0, server_tool_use=None, iterations=None)
    base.update(kw)
    return NS(**base)


def _response(stop="end_turn", text="hello", model="claude-sonnet-5-5", usage=None, details=None):
    return NS(stop_reason=stop, stop_details=details, model=model,
              content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
              usage=usage or _usage())


def test_log_anthropic_single_attempt():
    resp = _response(usage=_usage(server_tool_use=NS(web_search_requests=2)))
    cost = llm_usage.log_anthropic(resp, stage="price", item_id="abc")
    (row,) = _rows()
    assert (row["provider"], row["model"], row["stage"], row["item_id"]) == \
        ("anthropic", "claude-sonnet-5-5", "price", "abc")
    assert (row["input_tokens"], row["output_tokens"], row["web_searches"]) == (1000, 200, 2)
    assert row["cost_usd"] == pytest.approx(cost)
    assert cost == pytest.approx((1000 * 2 + 200 * 10) / 1e6 + 0.02)


def test_log_anthropic_refusal_fallback_bills_each_model():
    iterations = [
        NS(type="message", model="claude-sonnet-5-5", input_tokens=1000, output_tokens=0,
           cache_read_input_tokens=0, cache_creation_input_tokens=0),
        NS(type="fallback_message", model="claude-sonnet-5", input_tokens=1000,
           output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0),
    ]
    resp = _response(model="claude-sonnet-5", usage=_usage(iterations=iterations))
    llm_usage.log_anthropic(resp, stage="price")
    assert [r["model"] for r in _rows()] == ["claude-sonnet-5-5", "claude-sonnet-5"]


def test_logging_failure_never_raises(monkeypatch):
    def boom(**kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "record_llm_usage", boom)
    llm_usage.log_anthropic(_response(), stage="x")
    llm_usage.log_gemini(NS(usage_metadata=NS(prompt_token_count=1, candidates_token_count=1),
                            candidates=[]), model="gemini-2.5-flash")


class _GeminiStub:
    def __init__(self, response):
        self.models = NS(generate_content=lambda **kw: response)


def test_generate_with_retry_logs_gemini_and_stays_backward_compatible():
    resp = NS(
        usage_metadata=NS(prompt_token_count=10_000, candidates_token_count=500,
                          thoughts_token_count=1500, cached_content_token_count=None,
                          tool_use_prompt_token_count=2000),
        candidates=[NS(grounding_metadata=NS(web_search_queries=["a", "b", "c"]))],
    )
    # Old call shape: no stage / item_id.
    assert llm.generate_with_retry(_GeminiStub(resp), model="gemini-3.5-flash",
                                   contents="x") is resp
    llm.generate_with_retry(_GeminiStub(resp), model="gemini-2.5-flash", contents="x",
                            stage="draft", item_id="it1")
    old, new = _rows()
    assert old["stage"] is None and old["provider"] == "gemini"
    assert (old["input_tokens"], old["output_tokens"], old["web_searches"]) == (12_000, 2000, 3)
    assert old["cost_usd"] == pytest.approx((12_000 * 0.75 + 2000 * 3.75) / 1e6)
    assert (new["stage"], new["item_id"]) == ("draft", "it1")
    assert new["cost_usd"] == pytest.approx((12_000 * 0.30 + 2000 * 2.50) / 1e6)


# --- claude_llm.ask ----------------------------------------------------------

class _ClaudeStub:
    def __init__(self, response):
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))
        self._response = response

    def _create(self, **params):
        self.calls.append(params)
        return self._response


def test_ask_returns_text_and_logs():
    stub = _ClaudeStub(_response(text=" fine "))
    assert claude_llm.ask("t", "hi", client=stub, max_tokens=50) == "fine"
    params = stub.calls[0]
    assert params["model"] == config.CLAUDE_MODEL
    assert params["messages"] == [{"role": "user", "content": "hi"}]
    assert params["fallbacks"] == "default"
    assert "thinking" not in params and "output_config" not in params
    assert _rows()[0]["stage"] == "t"


def test_ask_structured_output_effort_and_images():
    stub = _ClaudeStub(_response(text='{"price": 24.99}', model="claude-haiku-5-5"))
    schema = {"type": "object", "properties": {"price": {"type": "number"}},
              "required": ["price"], "additionalProperties": False}
    out = claude_llm.ask("price", [{"role": "user", "content": "price it"}], client=stub,
                         model="claude-haiku-5-5", effort="low", output_schema=schema,
                         images=[b"\x89PNG fake"], system="be brief")
    assert out == {"price": 24.99}
    params = stub.calls[0]
    assert params["output_config"] == {"effort": "low",
                                       "format": {"type": "json_schema", "schema": schema}}
    assert "fallbacks" not in params and "betas" not in params  # haiku has no fallback
    assert params["system"] == "be brief"
    blocks = params["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[1] == {"type": "text", "text": "price it"}


def test_ask_downgrades_when_over_budget():
    _spend(97.0, when=datetime.now(timezone.utc))
    stub = _ClaudeStub(_response())
    claude_llm.ask("t", "hi", client=stub, model="claude-opus-5-5")
    assert stub.calls[0]["model"] == "claude-sonnet-5-5"


def test_ask_raises_on_refusal_and_max_tokens_after_logging():
    refused = _response(stop="refusal", text="",
                        details=NS(category="cyber", explanation="declined"))
    with pytest.raises(claude_llm.ClaudeRefusal) as exc:
        claude_llm.ask("t", "hi", client=_ClaudeStub(refused))
    assert exc.value.category == "cyber"
    with pytest.raises(claude_llm.ClaudeTruncated):
        claude_llm.ask("t", "hi", client=_ClaudeStub(_response(stop="max_tokens")))
    assert len(_rows()) == 2  # both billed attempts are on the books


def test_ask_rejects_invalid_json():
    with pytest.raises(claude_llm.ClaudeError):
        claude_llm.ask("t", "hi", client=_ClaudeStub(_response(text="not json")),
                       output_schema={"type": "object"})


def test_usage_report_groups_by_model_and_stage():
    now = datetime.now(timezone.utc)
    db.record_llm_usage(provider="anthropic", model="claude-sonnet-5-5", stage="price",
                        input_tokens=1000, output_tokens=100, cost_usd=0.5, ts=now.isoformat())
    db.record_llm_usage(provider="gemini", model="gemini-9-pro", stage="research",
                        input_tokens=10, output_tokens=1, cost_usd=None, ts=now.isoformat())
    text = "\n".join(llm_usage.usage_report())
    assert "Anthropic: $0.50 of $100 budget (0%)" in text
    assert "claude-sonnet-5-5 · price: $0.500" in text
    assert "gemini-9-pro · research: unpriced" in text
