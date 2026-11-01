"""Price notes: short facts from identify research shown at the price gate and draft."""
import identify
from bot import formatting


def test_clean_keeps_four_and_drops_junk():
    notes = [
        {"text": "  Reddit  'grail find'  post ", "url": "https://reddit.com/r/x"},
        {"text": "none found", "url": None},
        {"text": "", "url": "https://a"},
        "Bridle leather (tough horse-gear leather)",
        {"text": "bad link", "url": "javascript:alert(1)"},
        {"text": "four", "url": None},
        {"text": "five", "url": None},
        42,
    ]
    out = identify._clean_value_notes(notes)
    assert [n["text"] for n in out] == [
        "Reddit 'grail find' post", "Bridle leather (tough horse-gear leather)",
        "bad link", "four"]
    assert out[0]["url"] == "https://reddit.com/r/x"
    assert out[1]["url"] is None and out[2]["url"] is None


def test_clean_handles_missing_or_wrong_type():
    assert identify._clean_value_notes(None) == []
    assert identify._clean_value_notes("none found") == []


def test_prompts_ask_for_notes_with_explanations():
    assert "Reddit" in identify.RESEARCH_PROMPT
    assert "value_notes" in identify.FORMAT_INSTRUCTIONS
    assert "parentheses" in identify.FORMAT_INSTRUCTIONS


def test_price_gate_shows_notes_with_links():
    ident = {"value_notes": [
        {"text": "Whitehouse Cox (old English leather maker) x Polo collab", "url": None},
        {"text": "Reddit: posted as a 'grail find'", "url": "https://reddit.com/r/x"}]}
    text = formatting.format_pricing({"suggested_price": 169.99}, ident)
    assert "📝 Price notes:" in text
    assert "  • Whitehouse Cox (old English leather maker) x Polo collab" in text
    assert "    https://reddit.com/r/x" in text


def test_no_notes_leaves_screen_unchanged():
    pricing = {"suggested_price": 39.99}
    assert formatting.format_pricing(pricing) == formatting.format_pricing(pricing, {})
    assert "Price notes" not in formatting.format_pricing(pricing, {"value_notes": []})


def test_draft_shows_notes():
    result = {
        "identification": {"brand": "Polo", "model": "Belt", "confidence": 0.9,
                           "value_notes": [{"text": "Rarely resold", "url": None}]},
        "pricing": {"suggested_price": 169.99},
        "listing": {"title": "Polo Belt", "price": 169.99, "description": "d"},
    }
    assert "  • Rarely resold" in formatting.format_draft(result)
