"""pipeline/listing_check.py: issue parsing, the empty and failure results, the
prompt it builds, and the review-screen block.

No network: Claude is a stub passed as `ask`, and photo ordering is replaced so
the eBay module (and its DB) is never imported.

Run with `python tests/test_listing_check.py` or `pytest tests/`.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import claude_llm
import config
from pipeline import listing_check as lc

lc._photo_order = lambda item: list(item.get("photos") or [])


def _item(photos=()):
    return {
        "item_id": "abc12345-0000",
        "photos": list(photos),
        "identification": {"brand": "Kate Spade New York", "color": "Navy", "size": "Small"},
        "pricing": {"claude_price": 59.99, "claude_confidence": "medium",
                    "review_flags": ["priced_from_asks"],
                    "claude_evidence": {"reason": "two same-item sales near $60",
                                        "comps": [{"price": 64.5, "source": "sold",
                                                   "title": "Kate Spade Dolly Duffle Navy"}]}},
        "listing": {"title": "Kate Spade New York Dolly Small Duffle Crossbody Women's",
                    "description": "**SPECIFICATIONS:**\n- Brand: Kate Spade<br>Colour &amp; more",
                    "item_specifics": {"Brand": "Kate Spade New York", "Color": "Navy",
                                       "MPN": None},
                    "condition_id": "1000", "price": 59.99},
    }


class _Ask:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, stage, prompt, **kw):
        self.calls.append((stage, prompt, kw))
        if kw.get("meta") is not None:
            kw["meta"].update(model="claude-sonnet-5-5", cost_usd=0.0123,
                              input_tokens=2800, output_tokens=90)
        if self.error:
            raise self.error
        return self.answer


def test_parse_keeps_valid_issues_fix_first():
    raw = {"issues": [
        {"severity": "consider", "field": "price", "message": "Comp looks like a tote"},
        {"severity": "fix", "field": "title", "message": 'Title missing colour "Navy" (in specifics)'},
    ]}
    out = lc.parse_issues(raw)
    assert [i["severity"] for i in out] == ["fix", "consider"]
    assert out[0] == {"severity": "fix", "field": "title",
                      "message": 'Title missing colour "Navy" (in specifics)'}


def test_parse_drops_malformed_caps_and_truncates():
    raw = {"issues": [
        {"severity": "urgent", "field": "title", "message": "bad severity"},
        {"severity": "fix", "field": "shipping", "message": "bad field"},
        {"severity": "fix", "field": "title", "message": "   "},
        "not a dict",
    ] + [{"severity": "consider", "field": "specifics", "message": "x" * 300}] * 7}
    out = lc.parse_issues(raw)
    assert len(out) == lc.MAX_ISSUES
    assert all(len(i["message"]) <= lc.MESSAGE_MAX for i in out)
    assert out[0]["message"].endswith("…")


def test_parse_tolerates_garbage():
    assert lc.parse_issues(None) == []
    assert lc.parse_issues([]) == []
    assert lc.parse_issues({"issues": "none"}) == []


def test_empty_result_has_no_issues_and_is_checked():
    ask = _Ask({"issues": []})
    res = lc.check_listing(_item(), ask=ask)
    assert res["issues"] == [] and res["unchecked"] is None
    assert res["cost_usd"] == 0.0123 and res["model"] == "claude-sonnet-5-5"
    assert lc.format_check(res) is None


def test_failure_returns_unchecked_not_raise():
    for err in (TimeoutError("slow"),
                claude_llm.ClaudeRefusal("claude-sonnet-5-5", "cyber", None),
                claude_llm.ClaudeError("bad json")):
        res = lc.check_listing(_item(), ask=_Ask(error=err))
        assert res["issues"] == []
        assert res["unchecked"] == type(err).__name__
        assert res["cost_usd"] == 0.0123  # billed tokens still reported
        assert "didn't run" in lc.format_check(res)


def test_disabled_and_no_draft_skip_the_call():
    ask = _Ask({"issues": []})
    old = config.LISTING_CHECK_ENABLED
    try:
        config.LISTING_CHECK_ENABLED = False
        assert lc.check_listing(_item(), ask=ask)["unchecked"] == "disabled"
    finally:
        config.LISTING_CHECK_ENABLED = old
    assert lc.check_listing({"listing": None}, ask=ask)["unchecked"] == "no draft"
    assert ask.calls == []
    assert lc.format_check({"issues": [], "unchecked": "disabled"}) is None


def test_request_carries_listing_evidence_and_photos():
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n in range(5):
            p = Path(d) / f"p{n}.jpg"
            Image.new("RGB", (2000, 1500), (n * 40, 0, 0)).save(p)
            paths.append(str(p))
        paths.append(str(Path(d) / "missing.jpg"))
        ask = _Ask({"issues": []})
        lc.check_listing(_item(paths), ask=ask)
    stage, prompt, kw = ask.calls[0]
    assert stage == "listing_check"
    assert kw["model"] == config.LISTING_CHECK_MODEL and kw["effort"] == config.LISTING_CHECK_EFFORT
    assert kw["output_schema"] is lc.OUTPUT_SCHEMA and kw["item_id"] == "abc12345-0000"
    assert kw["system"][0]["text"] == lc.SYSTEM_PROMPT
    # cover, second and last existing photo, downscaled to <= 768px
    assert len(kw["images"]) == 3
    with Image.open(__import__("io").BytesIO(kw["images"][0][0])) as im:
        assert max(im.size) <= lc.PHOTO_SIDE
    for needle in ("Dolly Small Duffle", "- Color: Navy", "New with tags", "$59.99",
                   "confidence medium", "priced_from_asks", "Kate Spade Dolly Duffle Navy",
                   "two same-item sales", "brand=Kate Spade New York"):
        assert needle in prompt, needle
    assert "MPN" not in prompt          # empty specifics left out
    assert "<br>" not in prompt and "Colour & more" in prompt
    assert "**SPECIFICATIONS" not in prompt  # emphasis the renderer strips is stripped here too


def test_pick_photos_keeps_cover_and_last():
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for n in range(6):
            p = Path(d) / f"{n}.jpg"
            p.write_bytes(b"x")
            paths.append(str(p))
        picked = [p.name for p in lc.pick_photos(paths, 3)]
        assert picked == ["0.jpg", "1.jpg", "5.jpg"]
        assert len(lc.pick_photos(paths[:2], 3)) == 2


def test_review_block_format():
    res = {"issues": [{"severity": "fix", "field": "title",
                       "message": 'Title missing colour "Navy" (in specifics)'},
                      {"severity": "consider", "field": "price",
                       "message": "Comp looks like a different model"}],
           "unchecked": None}
    assert lc.format_check(res) == (
        "⚠️ Check:\n"
        '  • [fix] Title missing colour "Navy" (in specifics)\n'
        "  • [consider] Comp looks like a different model")


def test_format_draft_shows_block_only_with_issues():
    import telegram_bot
    item = _item()
    result = {"identification": {"brand": "Kate Spade", "model": "Dolly", "confidence": 0.9},
              "pricing": item["pricing"], "listing": item["listing"]}
    assert "Check:" not in telegram_bot.format_draft(result)
    result["listing_check"] = {"issues": [], "unchecked": None}
    assert "Check:" not in telegram_bot.format_draft(result)
    result["listing_check"] = {"issues": [{"severity": "fix", "field": "title",
                                           "message": 'Title missing colour "Navy"'}]}
    text = telegram_bot.format_draft(result)
    assert '⚠️ Check:\n  • [fix] Title missing colour "Navy"' in text
    assert text.index("Check:") < text.index("DESCRIPTION:")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
