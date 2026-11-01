"""Price notes: short price-moving facts from Claude + web search, shown at the
price gate and in the draft."""
from bot import formatting
from pipeline import value_notes


def test_clean_keeps_four_and_drops_junk():
    notes = [
        {"text": "  Reddit  'grail find'  thread ", "effect": "up",
         "url": "https://reddit.com/r/x/comments/1"},
        {"text": "none found", "url": None},
        {"text": "", "url": "https://a.com/b"},
        "Discontinued in 2022",
        {"text": "home page link", "effect": "sideways", "url": "https://www.oakley.com/"},
        {"text": "four", "effect": "down", "url": "javascript:alert(1)"},
        {"text": "five", "url": None},
        42,
    ]
    out = value_notes.clean(notes)
    assert [n["text"] for n in out] == [
        "Reddit 'grail find' thread", "Discontinued in 2022", "home page link", "four"]
    assert out[0] == {"text": "Reddit 'grail find' thread", "effect": "up",
                      "url": "https://reddit.com/r/x/comments/1"}
    assert out[2]["url"] is None and out[2]["effect"] is None   # home page, bad effect
    assert out[3]["url"] is None and out[3]["effect"] == "down"


def test_ebay_listing_notes_dropped():
    out = value_notes.clean([
        {"text": "Listed at $170", "effect": "down", "url": "https://www.ebay.com/itm/117494290448"},
        {"text": "Retail $298", "effect": "up", "url": None}])
    assert [n["text"] for n in out] == ["Retail $298"]


def test_long_note_cut_at_a_word():
    out = value_notes.clean([{"text": " ".join(f"w{i}" for i in range(30)), "url": None}])
    assert out[0]["text"] == " ".join(f"w{i}" for i in range(value_notes.MAX_WORDS)) + "…"


def test_clean_handles_missing_or_wrong_type():
    assert value_notes.clean(None) == []
    assert value_notes.clean("none found") == []


def test_find_notes_asks_claude_with_web_search():
    seen = {}

    def fake_ask(stage, prompt, **kw):
        seen.update(stage=stage, prompt=prompt, **kw)
        return {"notes": [{"text": "Retail $395", "effect": "up", "url": None}]}

    notes = value_notes.find_notes({"brand": "Polo", "product_name": "Braided Belt"},
                                   item_id="abc", ask=fake_ask)
    assert notes == [{"text": "Retail $395", "effect": "up", "url": None}]
    assert seen["stage"] == "value_notes" and seen["item_id"] == "abc"
    assert seen["tools"][0]["name"] == "web_search"
    assert "Polo" in seen["prompt"] and "Reddit" in seen["prompt"]
    assert "popular with X" in seen["prompt"]   # vague notes are ruled out


def test_find_notes_failure_returns_empty():
    def broken(*a, **k):
        raise RuntimeError("down")
    assert value_notes.find_notes({"brand": "X"}, ask=broken) == []


def test_price_gate_shows_notes_with_marks_and_links():
    pricing = {"suggested_price": 169.99, "value_notes": [
        {"text": "Whitehouse Cox (old English leather maker) factory closed 2022",
         "effect": "up", "url": None},
        {"text": "Strap cracks after a year, buyers say", "effect": "down",
         "url": "https://reddit.com/r/x/comments/1"}]}
    text = formatting.format_pricing(pricing)
    assert "📝 Price notes:" in text
    assert "  ⬆️ Whitehouse Cox (old English leather maker) factory closed 2022" in text
    assert "  ⬇️ Strap cracks after a year, buyers say" in text
    assert "    https://reddit.com/r/x/comments/1" in text


def test_no_notes_leaves_screen_unchanged():
    assert "Price notes" not in formatting.format_pricing({"suggested_price": 39.99})
    assert "Price notes" not in formatting.format_pricing(
        {"suggested_price": 39.99, "value_notes": []})


def test_draft_shows_notes():
    result = {
        "identification": {"brand": "Polo", "model": "Belt", "confidence": 0.9},
        "pricing": {"suggested_price": 169.99,
                    "value_notes": [{"text": "Rarely resold", "effect": "up", "url": None}]},
        "listing": {"title": "Polo Belt", "price": 169.99, "description": "d"},
    }
    assert "  ⬆️ Rarely resold" in formatting.format_draft(result)


def test_price_stage_saves_notes(monkeypatch):
    import config
    import db
    from pipeline import stages
    item_id = db.create_item(["p.jpg"])
    db.update_field(item_id, "identification", {"brand": "Polo", "search_query": "polo belt"})
    monkeypatch.setattr(config, "PRICING_ENGINE", "v1")
    monkeypatch.setattr(config, "VALUE_NOTES_ENABLED", True)
    monkeypatch.setattr(stages.price, "get_pricing", lambda q, research=None: {"suggested_price": 1})
    monkeypatch.setattr(value_notes, "find_notes",
                        lambda ident, item_id=None: [{"text": "t", "effect": "up", "url": None}])
    stages.stage_price(item_id)
    assert db.get_item(item_id)["pricing"]["value_notes"][0]["text"] == "t"
