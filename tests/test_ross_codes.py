"""Two Ross codes on one tag: the main tag's and the REDUCED sticker's."""
import pytest

import config
import db
import ross_tag
from pipeline import check, owned

STICKER, MAIN = "400111222333", "400444555666"


@pytest.fixture(autouse=True)
def own_db(monkeypatch, tmp_path):
    if config.DB_BACKEND != "sqlserver":
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "codes.db"))
        db.create_tables()
    monkeypatch.setattr(config, "CHECK_OWNED_AI", False)


def bar(code, cents):
    return f"{code}{cents:06d}"


def test_split_codes_sticker_is_the_lowest_price():
    got = ross_tag.split_codes([bar(MAIN, 2999), bar(STICKER, 999)])
    assert (got["code"], got["reduced_price"], got["main_code"], got["main_price"]) == (STICKER, 9.99, MAIN, 29.99)
    assert got["barcode"] == bar(STICKER, 999)
    one = ross_tag.split_codes([bar(STICKER, 999)])
    assert (one["code"], one["main_code"], one["main_price"]) == (STICKER, None, None)
    same = ross_tag.split_codes([bar(STICKER, 2999), bar(STICKER, 999)])     # one code, marked down
    assert (same["code"], same["reduced_price"], same["main_code"], same["main_price"]) == (STICKER, 9.99, None, 29.99)
    assert ross_tag.split_codes(["123456789012000999"])["code"] is None       # not a Ross code
    assert ross_tag.split_codes([])["code"] is None


def test_extract_receipt_keeps_both_codes(monkeypatch, tmp_path):
    monkeypatch.setattr(ross_tag, "decode_barcodes", lambda p: [bar(MAIN, 2999), bar(STICKER, 999)])
    monkeypatch.setattr(ross_tag, "ocr_text", lambda p: "")
    r = ross_tag.extract_receipt(str(tmp_path / "tag.jpg"))
    assert (r["code"], r["reduced_price"], r["main_code"], r["original_price"], r["source"]) == \
        (STICKER, 9.99, MAIN, 29.99, "barcode")
    monkeypatch.setattr(ross_tag, "decode_barcodes", lambda p: [bar(STICKER, 999)])
    assert "main_code" not in ross_tag.extract_receipt(str(tmp_path / "tag.jpg"))


ROTATED = __import__("pathlib").Path(__file__).parent.parent / "test_images" / "intake_tag_rotated_15deg.jpg"


@pytest.mark.skipif(ross_tag._zbar_decode is None, reason="pyzbar not installed")
def test_intake_reads_a_tag_that_decodes_only_rotated(monkeypatch):
    """Item 0d49692b (2026-11-05 fix): its tag decodes only at 15 degrees, so
    intake stored placeholder 000000000111 and a typed $10.00."""
    assert ross_tag.decode_barcode(str(ROTATED)) is None                 # the straight read fails
    assert ross_tag.read_barcodes(str(ROTATED)) == ["400252597608000999"]
    monkeypatch.setattr(ross_tag, "ocr_text", lambda p: "")              # the barcode alone must do it
    monkeypatch.setattr(ross_tag, "TAG_VISION_FALLBACK", False)
    r = ross_tag.extract_receipt(str(ROTATED))
    assert (r["code"], r["reduced_price"], r["source"]) == ("400252597608", 9.99, "barcode")
    assert check.read_ross_barcode(str(ROTATED)) == {"code": "400252597608", "reduced_price": 9.99}


from pipeline import inventory as _inventory  # noqa: E402

# The real read_tag, taken at collection: tests/test_inventory.py replaces
# inventory.read_tag with fakes while it runs (and doesn't put it back).
_REAL_READ_TAG = _inventory.read_tag


@pytest.mark.skipif(ross_tag._zbar_decode is None, reason="pyzbar not installed")
def test_inv_piece_reads_a_rotated_tag(monkeypatch):
    """/inv (pipeline/inventory.read_tag) uses the same rotation-aware reader."""
    import llm
    monkeypatch.setattr(llm, "generate_with_retry", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no AI")))
    info = _REAL_READ_TAG(str(ROTATED))
    assert (info["ross_code"], info["ross_price"], info["ross_barcode"]) == \
        ("400252597608", 9.99, "400252597608000999")
    assert "ross_code_main" not in info


def test_decode_barcodes_needs_a_first_barcode(monkeypatch):
    monkeypatch.setattr(ross_tag, "decode_barcode", lambda p: None)
    assert ross_tag.decode_barcodes("nothing.jpg") == []


def test_read_ross_barcode_and_photo_roles(monkeypatch):
    monkeypatch.setattr(ross_tag, "decode_barcodes", lambda p: [bar(MAIN, 2999), bar(STICKER, 999)])
    assert check.read_ross_barcode("tag.jpg") == {"code": STICKER, "reduced_price": 9.99, "main_code": MAIN}
    roles = check.photo_roles(["item.jpg", "tag.jpg"],
                              decode=lambda p: {"code": STICKER, "reduced_price": 9.99, "main_code": MAIN}
                              if p == "tag.jpg" else {}, vision=lambda p: {})
    assert (roles["sku"], roles["sku_main"], roles["price"]) == (STICKER, MAIN, 9.99)


def test_photo_roles_main_tag_in_another_photo(monkeypatch):
    # photo 1 reads as the main tag; photo 2 has the REDUCED sticker (lower price)
    monkeypatch.setattr(ross_tag, "decode_barcodes", lambda p: [bar(STICKER, 999)] if p == "b.jpg" else [])
    roles = check.photo_roles(["a.jpg", "b.jpg", "item.jpg"],
                              decode=lambda p: {"code": MAIN, "reduced_price": 29.99} if p == "a.jpg" else {},
                              vision=lambda p: {})
    assert (roles["sku"], roles["price"], roles["sku_main"]) == (STICKER, 9.99, MAIN)


# --- stored on both backends, matched on either -------------------------------------------

def test_stored_and_read_back():
    item = db.create_item([])
    db.update_field(item, "receipt", {"code": STICKER, "main_code": MAIN, "reduced_price": 9.99})
    assert db.get_item(item)["receipt"]["main_code"] == MAIN
    code = db.add_piece("O1W")
    db.update_piece(code, info={"brand": "Vans", "ross_code": STICKER, "ross_code_main": MAIN})
    assert db.get_piece(code)["info"]["ross_code_main"] == MAIN
    cid = db.add_check(user_id=1, ross_sku=STICKER, ross_sku_main=MAIN)
    assert db.get_check(cid)["ross_sku_main"] == MAIN


@pytest.mark.parametrize("theirs_main", [True, False])
def test_already_have_matches_on_either_code(theirs_main):
    item = db.create_item([])
    # the item knows both codes; the check sees one of them (or the reverse)
    db.update_field(item, "receipt", {"code": STICKER, "main_code": MAIN} if theirs_main else {"code": STICKER})
    db.update_status(item, "published")
    sku = [MAIN] if theirs_main else [MAIN, STICKER]
    [h] = owned.lookup({}, sku)["hits"]
    assert (h["id"], h["match"]) == (item, owned.ROSS)


def test_piece_and_check_main_codes_match():
    code = db.add_piece("O1W")
    db.update_piece(code, info={"ross_code": STICKER, "ross_code_main": MAIN})
    cid = db.add_check(user_id=1, ross_sku=STICKER, ross_sku_main=MAIN)
    got = {(h["source"], h["id"]) for h in owned.lookup({}, [MAIN])["hits"]}
    assert got == {("piece", code), ("check", cid)}


def test_receipts_are_asked_for_both_codes():
    asked = []
    owned.lookup({}, [STICKER, MAIN], receipts_fn=lambda skus: asked.append(skus) or [])
    assert asked == [sorted([STICKER, MAIN])]
