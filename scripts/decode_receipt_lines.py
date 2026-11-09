"""Decode every Ross receipt line description into receipt_line_details
(docs/receipt-decoding.md). Three stages; only the last one writes:

    # 1. free dry run: tiers, keys left for the AI, estimated cost
    python scripts/decode_receipt_lines.py --target ResaleDB --production
    # 2. paid AI run: decodes to a JSONL + a report with a 30-line sample; nothing goes to the DB
    python scripts/decode_receipt_lines.py --target ResaleDB --production --ai
    # 3. after the sample is approved: back up, then save
    python scripts/decode_receipt_lines.py --target ResaleDB --production --apply --from <jsonl>

Targets: ResaleDB (with --production), ResaleDB_Dev, ResaleDB_Test_*.
Receipts, receipt lines and our items are only read. The AI's token usage is
logged to the database in .env (SQL_DATABASE, the dev one), never the target;
the script totals the cost itself. Stage 3 needs migration step 5 on the
target (the bot's deploy runs it), backs the target up, and writes only
receipt_line_details: the AI rows from the JSONL (never paid twice) plus the
free matched-item and rules rows, worked out again from the target as it is.
"""
import argparse
import json
import math
import random
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import sqlserver  # noqa: E402

SEED = 20261108
SAMPLE = 30


def check_target(name: str, production: bool) -> None:
    sqlserver._check_name(name)
    if name.casefold() == sqlserver.PRODUCTION_DATABASE.casefold():
        if not production:
            raise SystemExit(f"refusing {name!r}: the production database needs --production")
        return
    if production:
        raise SystemExit(f"--production is only for {sqlserver.PRODUCTION_DATABASE!r}, not {name!r}")
    if name == sqlserver.DEV_DATABASE:
        return
    if name.startswith(sqlserver.TEST_PREFIX):
        sqlserver.assert_test_database(name)
        return
    raise SystemExit(f"refusing {name!r}: use {sqlserver.PRODUCTION_DATABASE} (--production), "
                     f"{sqlserver.DEV_DATABASE} or a {sqlserver.TEST_PREFIX}* database")


@contextmanager
def reading(target: str):
    """Point db.py at the target for the reads (and the final save) only, so
    the AI's llm_usage rows still go to the .env database."""
    old = config.SQL_DATABASE
    config.SQL_DATABASE = target
    try:
        yield
    finally:
        config.SQL_DATABASE = old


def load(target: str):
    import db
    from pipeline import owned
    with reading(target):
        data = db.receipt_decode_inputs()
        hits = owned.ross_hits({ln["sku"] for ln in data["lines"] if ln.get("sku")})
    return data, hits


# --- numbers ----------------------------------------------------------------------------------

def tier_counts(lines: list, tiers: dict) -> dict:
    keys, n_lines = Counter(tiers.values()), Counter()
    for ln in lines:
        n_lines[tiers.get(ln.get("desc_key")) or "blank"] += 1
    return {t: (keys.get(t, 0), n_lines.get(t, 0)) for t in ("matched_item", "rules", "ai", "skip", "blank")}


def estimate_cost(n: int) -> dict:
    """Rough cost of decoding n descriptions on the AI model."""
    from pipeline import receipt_decode as rd
    model = config.RECEIPT_DECODE_MODEL or config.CLAUDE_CHEAP_MODEL
    rates = config.ANTHROPIC_PRICES.get(model) or max(config.ANTHROPIC_PRICES.values(), key=lambda r: r["out"])
    calls = math.ceil(n / config.RECEIPT_DECODE_BATCH) if n else 0
    fixed = len(rd._SYSTEM) // 3 + len(json.dumps(rd._ai_schema())) // 3
    tokens_in = calls * fixed + n * 30
    tokens_out = int(n * 60 * 1.5)          # the answers, plus some thinking at effort low
    usd = (tokens_in * rates["in"] + tokens_out * rates["out"]) / 1e6
    return {"model": model, "calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "usd": round(usd, 4)}


def key_check(lines: list) -> int:
    """Lines whose SQL-made key differs from the Python mirror (strip+upper).
    Must be 0: the key is only ever made by SQL, this just proves they agree."""
    return sum(1 for ln in lines if ln.get("desc_key") and
               ln["desc_key"] != (ln.get("description") or "").strip().upper())


def _norm_brand(b) -> str:
    s = "".join(ch for ch in str(b or "").lower() if ch.isalnum())
    return {"jordan": "nike", "polo": "poloralphlauren", "ralphlauren": "poloralphlauren",
            "edhardy": "edhardy", "tommy": "tommyhilfiger"}.get(s, s)


def validate(ai_rows: dict, items: dict) -> dict:
    """The AI against our own items, on descriptions whose SKU is one of
    them. Brands compared loosely (case, spacing, Jordan = Nike)."""
    cat = brand = typ = n_cat = n_brand = n_typ = 0
    misses = []
    for key, item in items.items():
        a = ai_rows.get(key)
        if not a:
            continue
        if item.get("category"):
            n_cat += 1
            cat += a.get("category") == item["category"]
        if item.get("brand_guess"):
            n_brand += 1
            ok = bool(a.get("brand_guess")) and (
                _norm_brand(a["brand_guess"]) == _norm_brand(item["brand_guess"])
                or _norm_brand(item["brand_guess"]).startswith(_norm_brand(a["brand_guess"]))
                or _norm_brand(a["brand_guess"]).startswith(_norm_brand(item["brand_guess"])))
            brand += ok
        if item.get("item_type") and a.get("item_type"):
            n_typ += 1
            typ += a["item_type"] == item["item_type"]
        if item.get("category") and a.get("category") != item["category"]:
            misses.append((key, item, a))
    pct = lambda x, n: round(100 * x / n) if n else None  # noqa: E731
    return {"n": len([k for k in items if k in ai_rows]), "category": (cat, n_cat, pct(cat, n_cat)),
            "brand": (brand, n_brand, pct(brand, n_brand)), "type": (typ, n_typ, pct(typ, n_typ)),
            "misses": misses[:15]}


# --- the 30-line sample --------------------------------------------------------------------------

def decode_for(key: str, p: dict, details: dict, ai_rows: dict) -> dict | None:
    if p["tiers"].get(key) == "ai":
        return ai_rows.get(key) or details.get(key)
    return p["write"].get(key) or details.get(key)


def choose_sample(lines: list, p: dict, details: dict, ai_rows: dict, size: int = SAMPLE) -> list:
    """size lines: a third at random (to judge the overall error rate), the
    rest spread over the tiers: rules, AI (over categories, with 4 unsure
    ones), matched items and misread lines."""
    from pipeline import receipt_decode as rd
    rng = random.Random(SEED)
    pool = [ln for ln in lines if ln.get("desc_key") and decode_for(ln["desc_key"], p, details, ai_rows)]
    chosen, seen_keys = [], set()

    def take(cands, n, why):
        cands = [c for c in cands if c["desc_key"] not in seen_keys]
        rng.shuffle(cands)
        for c in cands[:n]:
            seen_keys.add(c["desc_key"])
            chosen.append((why, c))

    n_random = size // 3
    take(pool, n_random, "random")
    by_tier = lambda t: [ln for ln in pool if p["tiers"].get(ln["desc_key"]) == t]  # noqa: E731
    take(by_tier("rules"), 5, "rules")
    ai_lines = by_tier("ai")
    unsure = [ln for ln in ai_lines if (decode_for(ln["desc_key"], p, details, ai_rows) or {}).get("confidence")
              is not None and float(decode_for(ln["desc_key"], p, details, ai_rows)["confidence"]) < rd.LOW_CONFIDENCE]
    take(unsure, 4, "AI, unsure")
    by_cat = {}
    for ln in ai_lines:
        by_cat.setdefault((decode_for(ln["desc_key"], p, details, ai_rows) or {}).get("category"), []).append(ln)
    want_ai = 9 - sum(1 for w, _ in chosen if w == "AI, unsure")
    cats = sorted(by_cat, key=lambda c: -len(by_cat[c]))
    i = 0
    while want_ai > 0 and any(by_cat.values()) and i < 200:
        c = cats[i % len(cats)]
        before = len(chosen)
        take(by_cat[c], 1, "AI")
        want_ai -= len(chosen) - before
        i += 1
    take(by_tier("matched_item"), 3, "our item")
    misread = [ln for ln in lines if ln.get("desc_key") and [c for c, _ in rd.misread_reasons(ln, ln)
                                                             if c not in rd.INFO]]
    take(misread, size - len(chosen), "misread")
    return chosen[:size]


def sample_table(chosen: list, p: dict, details: dict, ai_rows: dict, hits: dict) -> list[str]:
    from pipeline import receipt_decode as rd
    out = ["| # | why | date | store | price | receipt line | decode | source | conf | what it is | misread? |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, (why, ln) in enumerate(chosen, 1):
        row = decode_for(ln["desc_key"], p, details, ai_rows) or {}
        dec = " / ".join(str(x) for x in (row.get("category"), row.get("item_type"), row.get("brand_guess"),
                                          row.get("color"), row.get("gender"), row.get("product_model")) if x)
        linked = bool((hits.get(ln.get("sku")) or {}).get("items"))
        flags = "; ".join(w for c, w in rd.misread_reasons(ln, ln, linked=linked) if c not in rd.INFO)
        price = f"${ln['price']:.2f}" if ln.get("price") is not None else "-"
        conf = f"{float(row['confidence']):.2f}" if row.get("confidence") is not None else "-"
        cell = lambda s: str(s or "").replace("|", "/")  # noqa: E731
        out.append(f"| {n} | {why} | {ln.get('receipt_date') or '-'} | {cell(ln.get('store_number'))} | {price} | "
                   f"`{cell((ln.get('description') or '').strip())}` | {cell(dec)} | {row.get('source') or '-'} | "
                   f"{conf} | {cell(rd.describe(row))} | {cell(flags)} |")
    return out


# --- main ------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", required=True)
    ap.add_argument("--production", action="store_true", help="allow --target ResaleDB")
    ap.add_argument("--ai", action="store_true", help="stage 2: paid AI decode to a JSONL (no DB write)")
    ap.add_argument("--apply", action="store_true", help="stage 3: save (needs --from)")
    ap.add_argument("--from", dest="from_file", help="the JSONL from stage 2")
    ap.add_argument("--max-usd", type=float, default=config.RECEIPT_DECODE_MAX_USD)
    ap.add_argument("--redo-ai", action="store_true", help="decode descriptions that already have an AI row")
    ap.add_argument("--reuse", help="stage 2: keep the AI rows of an earlier stage-2 JSONL (not paid again)")
    ap.add_argument("--redo-category", action="append", default=[],
                    help="with --reuse: decode this category's rows again (repeatable)")
    ap.add_argument("--redo-fashion", action="store_true",
                    help="with --reuse: decode again the fashion-code lines (SM, VB, EH...) that got a "
                         "non-fashion category")
    ap.add_argument("--report-dir", default=str(config.LOG_DIR))
    args = ap.parse_args(argv)
    check_target(args.target, args.production)
    if config.DB_BACKEND != "sqlserver":
        raise SystemExit("set DB_BACKEND=sqlserver (receipts are on SQL Server only)")
    if args.apply and not args.from_file:
        raise SystemExit("--apply needs --from <the stage-2 JSONL>")
    if args.apply and args.ai:
        raise SystemExit("--ai and --apply are separate stages")

    # db.py brings the .env database (dev) up to date at import; never the target.
    import db
    from pipeline import receipt_decode as rd

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    data, hits = load(args.target)
    lines, details = data["lines"], data["details"]
    p = rd.plan(lines, details, hits, redo_ai=args.redo_ai)
    counts = tier_counts(lines, p["tiers"])
    print(f"{args.target}: {len(lines)} lines, {len(p['tiers'])} descriptions, "
          f"step 5 table {'present' if data['has_table'] else 'NOT there yet'}, {len(details)} saved decodes")
    for t, (k, n) in counts.items():
        print(f"  {t:13} {k:5} descriptions {n:5} lines")

    if args.apply:
        return apply(args, db, rd, data, hits, p)

    validation_keys = [k for k in p["validate"] if k not in {a["key"] for a in p["ai"]}]
    est = estimate_cost(len(p["ai"]) + len(validation_keys))
    keys_off = key_check(lines)
    misread = Counter(c for ln in lines for c, _ in rd.misread_reasons(ln, ln) if c not in rd.INFO)
    rep = [f"# Receipt line decoding: {'AI RUN' if args.ai else 'DRY RUN'} on `{args.target}` ({stamp} UTC)", "",
           f"- {len(lines)} lines, {len(p['tiers'])} unique descriptions; {len(details)} decodes already saved; "
           f"table from migration step 5 {'present' if data['has_table'] else 'not created yet'}.",
           f"- SQL key vs Python mirror: {keys_off} difference(s) (must be 0).", "",
           "| tier | descriptions | lines |", "|---|---|---|"]
    rep += [f"| {t} | {k} | {n} |" for t, (k, n) in counts.items()]
    rep += ["", f"AI to do: {len(p['ai'])} descriptions + {len(validation_keys)} for the check against our items. "
                f"Estimate: {est['calls']} calls on {est['model']}, ~{est['input_tokens']:,} in / "
                f"~{est['output_tokens']:,} out tokens, **~${est['usd']:.2f}** (stops at ${args.max_usd:.2f}).",
            "", "Misread lines: " + (", ".join(f"{c} {n}" for c, n in misread.most_common()) or "none")]
    if not args.ai:
        path = report_dir / f"receipt-decode-{args.target}-{stamp}-dry-run.md"
        path.write_text("\n".join(rep) + "\n", encoding="utf-8")
        print(f"estimate ~${est['usd']:.2f} for {len(p['ai']) + len(validation_keys)} descriptions; "
              f"key differences {keys_off}\nreport: {path}")
        return 0

    # --- stage 2: the AI (usage logged to the .env database, not the target)
    pending = p["ai"] + [p["info"][k] for k in validation_keys]
    reused = load_reuse(args.reuse, set(args.redo_category), args.redo_fashion) if args.reuse else {}
    pending = [x for x in pending if x["key"] not in reused]
    print(f"AI: {len(pending)} descriptions ({len(validation_keys)} for the check, {len(reused)} reused), "
          f"cap ${args.max_usd:.2f}")
    ai_rows, spent = rd.ai_decode_all(pending, max_usd=args.max_usd)
    ai_rows = {**reused, **ai_rows}
    if args.reuse:
        rep += ["", f"Reused {len(reused)} AI rows from `{args.reuse}` (not paid again); decoded again: "
                    f"{', '.join(args.redo_category + (['fashion codes'] if args.redo_fashion else [])) or 'nothing'}."]
    check = validate(ai_rows, p["validate"])
    jsonl = report_dir / f"receipt-decode-{args.target}-{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"run": stamp, "target": args.target, "spent": spent}, default=str) + "\n")
        for key, row in ai_rows.items():
            f.write(json.dumps({"desc_key": key, "row": row, "validation_only": key in validation_keys},
                               ensure_ascii=False, default=str) + "\n")
    chosen = choose_sample(lines, p, details, ai_rows)
    rep += ["", "## AI run", "",
            f"- {spent['calls']} calls on {spent['model']}: {spent['input_tokens']:,} in / "
            f"{spent['output_tokens']:,} out tokens, **${spent['cost_usd']:.4f}**."
            + (f" Stopped: {spent['stopped']} ({spent['left']} left)." if spent.get("stopped") else ""),
            f"- Decoded {len(ai_rows)} of {len(pending)}.", f"- Results: `{jsonl}` (nothing saved to the DB).",
            "", "## Check against our own items", "",
            f"On {check['n']} descriptions whose SKU is one of our items (the AI didn't see the item):",
            f"- category {check['category'][0]}/{check['category'][1]} = {check['category'][2]}%",
            f"- brand {check['brand'][0]}/{check['brand'][1]} = {check['brand'][2]}%",
            f"- type {check['type'][0]}/{check['type'][1]} = {check['type'][2]}%",
            "- These are mostly bags; groceries and household are only tested by the sample below."]
    if check["misses"]:
        rep += ["", "Category misses:"] + [f"- `{k}`: ours {i.get('category')}/{i.get('item_type')}, "
                                           f"AI {a.get('category')}/{a.get('item_type')}" for k, i, a in check["misses"]]
    rep += ["", f"## Sample ({len(chosen)} lines)", ""] + sample_table(chosen, p, details, ai_rows, hits)
    path = report_dir / f"receipt-decode-{args.target}-{stamp}.md"
    path.write_text("\n".join(rep) + "\n", encoding="utf-8")
    print(f"spent ${spent['cost_usd']:.4f}; category {check['category'][2]}%, brand {check['brand'][2]}%, "
          f"type {check['type'][2]}%\nreport: {path}\njsonl:  {jsonl}")
    return 0


def load_reuse(path: str, redo: set, redo_fashion: bool = False) -> dict:
    """{desc_key: row} from an earlier stage-2 JSONL, minus the categories to
    decode again (and, with redo_fashion, the fashion-code lines whose
    category isn't a fashion one)."""
    from pipeline import receipt_decode as rd
    rows = {}
    with open(path, encoding="utf-8") as f:
        next(f)  # the run header
        for line in f:
            rec = json.loads(line)
            key = rec["desc_key"]
            if redo_fashion and rd.fashion_code(rd.normalise(key)[0]) and not rd.fashion_ok(rec["row"], key):
                continue
            if rec["row"].get("category") not in redo:
                rows[rec["desc_key"]] = rec["row"]
    return rows


def apply(args, db, rd, data, hits, p) -> int:
    if not data["has_table"]:
        raise SystemExit(f"{args.target} has no receipt_line_details yet (migration step 5 runs when the bot "
                         "with this code starts). Nothing written.")
    rows = dict(p["write"])
    ai_keys = {a["key"] for a in p["ai"]}
    used = 0
    with open(args.from_file, encoding="utf-8") as f:
        next(f)  # the run header
        for line in f:
            rec = json.loads(line)
            if rec["desc_key"] in ai_keys and not rec.get("validation_only"):
                rows[rec["desc_key"]] = rec["row"]
                used += 1
    if args.target in (sqlserver.PRODUCTION_DATABASE, sqlserver.DEV_DATABASE):
        bak = sqlserver.backup_database(args.target, label="before-receipt-decode")
        print(f"backup: {bak}")
    with reading(args.target):
        saved = db.save_receipt_line_details([{"desc_key": k, **r} for k, r in rows.items()])
    log = Path(args.report_dir) / "receipt_decode.jsonl"
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "target": args.target,
                            "saved": saved, "ai_rows": used, "from": args.from_file}) + "\n")
    print(f"saved {saved} rows to {args.target}.receipt_line_details ({used} from the AI file, "
          f"{saved - used} matched-item/rules); {len(ai_keys) - used} descriptions still without a decode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
