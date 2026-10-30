"""Brand stock photos must be the same product before they reach a listing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import stock_photo as sp  # noqa: E402

BELT = {"brand": "Polo Ralph Lauren", "model": "405898683001", "upc": "888188438217"}


def test_other_style_number_in_url_is_rejected_without_a_call():
    url = "https://x.com/images/xlarge/FW21/POLO/PRL405807144001FW21(2).jpg"
    assert sp.code_verdict(url, BELT) is False
    ok, why = sp.stock_photo_matches("s.jpg", url, BELT, ["a.jpg"],
                                     ask_fn=lambda *a, **k: 1 / 0)
    assert not ok and "different style" in why


def test_our_style_number_in_url_is_accepted():
    url = "https://x.com/POLO/PRL405898683001FW21.jpg"
    assert sp.code_verdict(url, BELT) is True


def test_timestamps_and_cdn_ids_dont_decide():
    url = "https://res.cloudinary.com/x/image/upload/v1793355417/abc.jpg"
    assert sp.code_verdict(url, BELT) is None
    assert sp.code_verdict("https://x.com/p/123456789012.jpg", {"brand": "X"}) is None


def test_claude_decides_when_codes_cant(monkeypatch):
    monkeypatch.setattr(sp, "_small", lambda p, side=768: (b"img", "image/jpeg"))
    seen = {}

    def fake(stage, prompt, **k):
        seen["n"] = len(k["images"])
        return {"same_product": False, "reason": "roller buckle, flat braid"}
    ok, why = sp.stock_photo_matches("s.jpg", "https://x.com/p.jpg", BELT,
                                     ["a.jpg", "b.jpg", "c.jpg", "d.jpg"], ask_fn=fake)
    assert not ok and "roller" in why and seen["n"] == 4   # 3 of ours + the stock


def test_errors_fail_closed(monkeypatch):
    monkeypatch.setattr(sp, "_small", lambda p, side=768: (b"img", "image/jpeg"))

    def boom(*a, **k):
        raise RuntimeError("api down")
    ok, why = sp.stock_photo_matches("s.jpg", "https://x.com/p.jpg", BELT, ["a.jpg"], ask_fn=boom)
    assert not ok and "failed" in why
    monkeypatch.setattr(sp, "_small", lambda p, side=768: None)
    assert sp.stock_photo_matches("s.jpg", "https://x.com/p.jpg", BELT, ["a.jpg"],
                                  ask_fn=lambda *a, **k: {"same_product": True, "reason": ""})[0] is False


def test_any_listed_difference_vetoes_a_yes(monkeypatch):
    monkeypatch.setattr(sp, "_small", lambda p, side=768: (b"img", "image/jpeg"))
    ok, why = sp.stock_photo_matches(
        "s.jpg", "https://x.com/p.jpg", BELT, ["a.jpg"],
        ask_fn=lambda *a, **k: {"same_product": True, "reason": "close enough",
                                "differences": ["roller buckle vs plain buckle"]})
    assert not ok and "roller" in why
