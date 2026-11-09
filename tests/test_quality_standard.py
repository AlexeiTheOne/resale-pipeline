"""Listing quality standard for new listings (pipeline/quality.py): one bag
type, a 4-6 sentence description with dimensions, Multicolor for printed
items, a warning on titles too similar to our own, and the 3-day impression
check. Real titles and the approved d7432192 description are used as cases."""
import asyncio
from datetime import datetime, timedelta, timezone

import config
import db
from bot import jobs
from ebay import analytics
from pipeline import draft as draft_mod
from pipeline import listing_check, quality, stages

FOOTER = ("SHIPPING:\nShips within 1 business day via USPS with tracking number.\n\n"
          "RETURNS:\n30-day return policy. Buyer pays return shipping.")
GOOD_DESC = (
    "New with tags: an Ed Hardy women's shoulder bag purse in black faux leather, covered in Ed Hardy's "
    "classic multicolor tattoo art with skulls and roses. Studs along the trim and a decorative chain with "
    "metal Ed Hardy tiger and heart charms give this handbag its signature edgy look. It has two shoulder "
    "straps, gunmetal-tone hardware, and a magnetic flap closure with a metal Ed Hardy logo plaque on the "
    "front. It measures about 11 x 9 x 5 inches, with room for a phone, wallet, keys and makeup without "
    "the bulk.\n\n" + FOOTER)
OLD_DESC = ("Ed Hardy Tattoo Print Satchel Bag in black.\n\nSPECIFICATIONS:\n- Brand: Ed Hardy\n"
            "- Bag Length: 11 inches\n\nFEATURES:\n✓ Studded trim\n\n" + FOOTER)
SPECS = {"Brand": "Ed Hardy", "Style": "Shoulder Bag", "Type": "Handbag", "Pattern": "Tattoo Print",
         "Bag Width": "11 in", "Bag Height": "9 in", "Bag Depth": "5 in"}


def _listing(**kw):
    base = {"title": "Ed Hardy Skulls & Roses Shoulder Bag Purse Black Tattoo Studded Charms NWT",
            "category_id": "169291", "description": GOOD_DESC, "item_specifics": dict(SPECS)}
    base.update(kw)
    return base


# --- one bag type ---------------------------------------------------------------

def test_bag_types_in_titles():
    assert quality.bag_types("Ed Hardy Skull Rhinestone Shoulder Crossbody Bag Blush Pink") == \
        ["shoulder bag", "crossbody"]
    assert quality.bag_types("DKNY Hadlee Medium Canvas Logo Tote Crossbody Off-White") == ["tote", "crossbody"]
    assert quality.bag_types("Ed Hardy Skulls & Roses Shoulder Bag Purse Black") == ["shoulder bag"]
    # a crossbody strap or shoulder strap is a feature, not a type
    assert quality.bag_types("satchel with a detachable crossbody strap and shoulder strap") == ["satchel"]
    assert quality.bag_types("Luv Betsey Weekender Duffle Bag") == ["duffel"]


def test_the_approved_listing_meets_the_bag_rule():
    assert quality.bag_type_issues(_listing()) == []


def test_two_types_in_the_title_is_a_fix():
    issues = quality.bag_type_issues(_listing(title="Ed Hardy Skull Rhinestone Shoulder Crossbody Bag Pink"))
    assert issues and issues[0]["field"] == "title" and issues[0]["redo"]
    assert "shoulder bag, crossbody" in issues[0]["message"]


def test_style_and_type_must_follow_the_title():
    specs = dict(SPECS, Style="Satchel/Top Handle Bag", Type="Satchel")
    msgs = [i["message"] for i in quality.bag_type_issues(_listing(item_specifics=specs))]
    assert any('Style "Satchel/Top Handle Bag"' in m and '"Shoulder Bag"' in m for m in msgs)
    assert any('Type "Satchel"' in m and '"Handbag"' in m for m in msgs)


def test_the_description_must_not_name_another_type():
    desc = GOOD_DESC.replace("shoulder bag purse", "satchel purse")
    issues = quality.bag_type_issues(_listing(description=desc))
    assert [i["field"] for i in issues] == ["description"]


def test_not_a_bag_category_is_left_alone():
    assert quality.bag_type_issues(_listing(category_id="54257",
                                            title="Pillowcase Tote Shoulder Crossbody")) == []


# --- description ----------------------------------------------------------------------

def test_the_approved_description_passes():
    assert quality.description_issues(_listing()) == []
    assert len(quality.sentences(quality.description_body(GOOD_DESC))) == 4


def test_the_old_bullet_format_fails():
    msgs = [i["message"] for i in quality.description_issues(_listing(description=OLD_DESC))]
    assert any("headings or bullets" in m for m in msgs)
    assert any("sentences" in m for m in msgs)


def test_missing_dimensions_are_flagged_when_known():
    desc = GOOD_DESC.replace("It measures about 11 x 9 x 5 inches, with room", "There is room")
    msgs = [i["message"] for i in quality.description_issues(_listing(description=desc))]
    assert any("dimensions" in m for m in msgs)
    # unknown dimensions: nothing to ask for
    specs = {k: v for k, v in SPECS.items() if not k.startswith("Bag ")}
    assert not any("dimensions" in i["message"]
                   for i in quality.description_issues(_listing(description=desc, item_specifics=specs)))


def test_inches_abbreviation_does_not_end_a_sentence():
    body = ("New with tags tote. It measures 11 x 9 in. wide at the base. Zip top closure here. "
            "Two inner pockets inside.")
    assert len(quality.sentences(body)) == 4


# --- Multicolor ------------------------------------------------------------------------

def test_printed_bag_gets_multicolor():
    specs = {"Color": "Black", "Exterior Color": "Black", "Pattern": "Tattoo Print"}
    assert quality.apply_multicolor(specs, "169291") == ["Color", "Exterior Color"]
    assert specs["Color"] == specs["Exterior Color"] == "Multicolor"


def test_printed_bag_without_exterior_color_gets_one():
    specs = {"Color": "Black", "Pattern": "Floral"}
    quality.apply_multicolor(specs, "169291")
    assert specs["Exterior Color"] == "Multicolor"


def test_solid_items_and_non_bags():
    specs = {"Color": "Black", "Exterior Color": "Black", "Pattern": "Solid"}
    assert quality.apply_multicolor(specs, "169291") == [] and specs["Color"] == "Black"
    specs = {"Color": "Blue", "Pattern": "Floral"}
    assert quality.apply_multicolor(specs, "54257") == ["Color"] and "Exterior Color" not in specs


def test_draft_specifics_cleanup_applies_multicolor(monkeypatch):
    monkeypatch.setattr(draft_mod, "category_aspects", lambda cid: [])
    d = {"category_id": "169291",
         "item_specifics": {"Brand": "Ed Hardy", "Color": "Black", "Exterior Color": "Black",
                            "Pattern": "Tattoo Print"}}
    draft_mod._clean_draft_specifics(d, {})
    assert d["item_specifics"]["Exterior Color"] == "Multicolor"


# --- similar titles ------------------------------------------------------------------------

LIVE = [("61e7c55b-x", "Ed Hardy Skull Rhinestone Shoulder Crossbody Bag Blush Pink Small Women's"),
        ("d60d9e01-x", "Michael Kors Everett Front Pocket Backpack Brown Leather Large Unisex NWT")]


def test_a_near_twin_of_a_live_title_is_flagged():
    issues = quality.similar_title_issues(
        {"title": "Ed Hardy Rhinestone Tiger Skull Crossbody Shoulder Bag Pink Small Women's"}, LIVE, "new")
    assert len(issues) == 1 and "61e7c55b" in issues[0]["message"] and not issues[0]["redo"]


def test_different_titles_and_itself_are_not_flagged():
    assert quality.similar_title_issues({"title": "Kate Spade Spade Flower Dolly Small Duffel"}, LIVE, "x") == []
    assert quality.similar_title_issues({"title": LIVE[0][1]}, LIVE, "61e7c55b-x") == []


# --- checks together, and the one automatic redo ------------------------------------------------

def test_redo_instruction_only_for_fixable_issues():
    assert quality.redo_instruction(quality.check({"listing": _listing()})) is None
    fix = quality.redo_instruction(quality.check({"listing": _listing(description=OLD_DESC)}))
    assert fix and "4-6 sentences" in fix


def test_stage_redoes_a_failing_draft_once(monkeypatch):
    calls = []

    def fake_revise(draft, correction, identification=None):
        calls.append(correction)
        return _listing()
    monkeypatch.setattr(draft_mod, "revise_draft", fake_revise)
    out = stages._meet_standard("abc", _listing(description=OLD_DESC), {}, lambda _t: None)
    assert len(calls) == 1 and out["description"] == GOOD_DESC
    calls.clear()
    stages._meet_standard("abc", _listing(), {}, lambda _t: None)
    assert calls == []


def test_a_failed_redo_keeps_the_draft(monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(draft_mod, "revise_draft", broken)
    first = _listing(description=OLD_DESC)
    assert stages._meet_standard("abc", first, {}, lambda _t: None) is first


def test_standard_issues_reach_the_review_card_with_the_ai_check_off(monkeypatch):
    item = {"item_id": "new1", "status": "review", "listing": _listing(description=OLD_DESC)}
    stored = {}
    monkeypatch.setattr(config, "LISTING_CHECK_ENABLED", False)
    monkeypatch.setattr(db, "get_item", lambda iid: item)
    monkeypatch.setattr(db, "list_items", lambda: [item])
    monkeypatch.setattr(db, "update_field", lambda iid, f, v: stored.update({f: v}))
    result = listing_check.check_and_store("new1")
    assert stored["listing_check"] is result
    assert any(i["source"] == "standard" for i in result["issues"])
    assert "⚠️ Check:" in listing_check.format_check(result)


# --- impressions at 3 days ------------------------------------------------------------------------

NOW = datetime(2026, 11, 9, 15, tzinfo=timezone.utc)


def _live(item_id, days_old, **ebay):
    return {"item_id": item_id, "status": "published", "listing": {"title": f"Bag {item_id}"},
            "ebay": {"listing_id": f"L{item_id}", "view_item_url": f"u/{item_id}",
                     "published_at": (NOW - timedelta(days=days_old)).isoformat(), **ebay}}


def test_only_listings_three_to_ten_days_old_and_unchecked_are_due():
    items = [_live("a", 1), _live("b", 3.2), _live("c", 9), _live("d", 30),
             _live("e", 4, impressions_check={"impressions": 5})]
    assert [i["item_id"] for i in quality.due_for_impression_check(items, NOW)] == ["b", "c"]


def test_zero_impressions_alert_and_unknown_is_not_stored():
    items = [_live("b", 3), _live("c", 4), _live("f", 5)]
    traffic = {"Lb": {"impressions": 0}, "Lc": {"impressions": 40}}   # f unknown
    lines, results = quality.zero_impression_alerts(items, traffic)
    assert results == {"b": {"impressions": 0}, "c": {"impressions": 40}}
    assert len(lines) == 2 and "1 new listing" in lines[0] and "b" in lines[1] and "u/b" in lines[1]


def test_traffic_missing_from_a_good_batch_is_zero_and_a_failed_batch_unknown(monkeypatch):
    def fake_report(ids, days):
        if "Lbad" in ids:
            raise RuntimeError("429")
        return {"La": {"impressions": 12}}
    monkeypatch.setattr(analytics, "get_traffic_report", fake_report)
    good = [_live("a", 3), _live("b", 3)]
    assert jobs.impression_traffic(good) == {"La": {"impressions": 12}, "Lb": {"impressions": 0}}
    assert jobs.impression_traffic([_live("bad", 3)], batch=1) == {}


def test_the_job_alerts_owners_and_stores_each_result_once(monkeypatch):
    item = _live("z", 3)
    item["ebay"]["published_at"] = (datetime.now(timezone.utc) - timedelta(days=3, hours=1)).isoformat()
    rows = {"z": item}
    sent, runs = [], []
    monkeypatch.setattr(db, "list_items", lambda: list(rows.values()))
    monkeypatch.setattr(db, "get_item", lambda iid: rows.get(iid))
    monkeypatch.setattr(db, "update_field", lambda iid, f, v: rows[iid].__setitem__(f, v))
    monkeypatch.setattr(db, "record_job_run", lambda job, **kw: runs.append((job, kw)))
    monkeypatch.setattr(analytics, "get_traffic_report", lambda ids, days: {})
    monkeypatch.setattr(config, "OWNER_USER_IDS", {77})

    async def fake_send(bot, uid, lines):
        sent.append((uid, lines))
    monkeypatch.setattr(jobs.util, "_send_chunked_to", fake_send)
    asyncio.run(jobs._run_impression_check(type("App", (), {"bot": None})()))
    assert sent and sent[0][0] == 77 and "0 impressions" in sent[0][1][1]
    assert rows["z"]["ebay"]["impressions_check"]["impressions"] == 0
    sent.clear()
    asyncio.run(jobs._run_impression_check(type("App", (), {"bot": None})()))
    assert sent == [], "checked once"
    assert runs[0][0] == jobs._JOB_IMPRESSIONS
