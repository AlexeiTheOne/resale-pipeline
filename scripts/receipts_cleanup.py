"""Receipts cleanup after the copy into ResaleDB (docs/schema-proposal.md,
section 4). A DRY RUN unless --apply is given.

    python scripts/receipts_cleanup.py --target ResaleDB_Migrate_20261102            # dry run
    python scripts/receipts_cleanup.py --target ResaleDB_Migrate_20261102 --apply
    python scripts/receipts_cleanup.py --target ResaleDB --production --apply     # cutover

What it changes (everything else is only listed for a person):
- Duplicate receipt numbers: the receipt with the most lines is kept. A line
  of another copy whose SKU is already on the kept receipt is deleted; any
  other line is moved onto it (next free line number). Then the copy is
  deleted.
- Duplicate line numbers on one receipt: the lines are split into import
  batches (the line number restarts at a new import). A batch is deleted
  only when every one of its lines (SKU, description, price) is also in the
  biggest batch. Otherwise the receipt is flagged.
- Decimal-loss prices (> $500, and price / 100 <= $500): fixed only with
  evidence: (a) the raw receipt text shows price / 100 after that SKU, or
  (b) dividing brings the line sum within $0.05 of the subtotal. The old
  price goes to PriceOriginal. The rest are flagged.

Every change is a row in receipt_fixes. The original storage/raw and
storage/parsed files are only read. Applying runs in one transaction and
stops if any row changed since it was planned. Afterwards the unique receipt
indexes (migration step 2) can be built: init_sqlserver_db.py, or the bot's
startup.
"""
import argparse
import importlib.util
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import migrations  # noqa: E402
import sqlserver  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "migrate_sqlite_to_mssql", Path(__file__).resolve().parent / "migrate_sqlite_to_mssql.py")
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)

DEFAULT_RAW_DIR = config.BASE_DIR.parent / "receipt_pipeline" / "storage" / "raw"
CENT = Decimal("0.01")
_SKU_RE = re.compile(r"\b\d{12}\b")
_PRICE_RE = re.compile(r"(\d{1,4})[.,](\d{2})(?!\d)")


# --- read ----------------------------------------------------------------------

def load(con):
    cur = con.cursor()
    cur.execute("SELECT ReceiptID, ReceiptNumber, ReceiptDate, Subtotal, Total, RawFileID, SourceFile "
                "FROM dbo.Receipts")
    cols = [c[0] for c in cur.description]
    receipts = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    cur.execute("SELECT ItemID, ReceiptID, LineIndex, SKU, Description, Price, PriceOriginal "
                "FROM dbo.ReceiptItems")
    cols = [c[0] for c in cur.description]
    lines = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    return receipts, lines


def _line_text(ln) -> str:
    desc = (ln["Description"] or "")[:40]
    return f"L{ln['LineIndex']} {ln['SKU'] or '-'} '{desc}' {ln['Price']}"[:200]


def _receipt_text(r, n_lines) -> str:
    return (f"{r['ReceiptNumber']} date {r['ReceiptDate']} subtotal {r['Subtotal']} "
            f"total {r['Total']} lines {n_lines}")[:200]


# --- plan ----------------------------------------------------------------------
# An action: {op, table, row_id, changes: [(column, old, new)], reason, why}.
# The plan is built against an in-memory copy, so each step sees the ones before.

def _by_receipt(lines):
    out = defaultdict(list)
    for ln in lines.values():
        out[ln["ReceiptID"]].append(ln)
    for v in out.values():
        v.sort(key=lambda ln: (ln["LineIndex"], ln["ItemID"]))
    return out


def plan_duplicate_receipts(receipts, lines, actions, flags):
    groups = defaultdict(list)
    for r in receipts.values():
        if r["ReceiptNumber"] is not None:
            groups[r["ReceiptNumber"]].append(r)
    for number, rs in sorted(groups.items()):
        if len(rs) < 2:
            continue
        by_r = _by_receipt(lines)
        keep = max(rs, key=lambda r: (len(by_r[r["ReceiptID"]]), -r["ReceiptID"]))
        kept_skus = Counter(ln["SKU"] for ln in by_r[keep["ReceiptID"]] if ln["SKU"])
        next_index = max((ln["LineIndex"] for ln in by_r[keep["ReceiptID"]]), default=0) + 1
        for other in sorted(rs, key=lambda r: r["ReceiptID"]):
            if other is keep:
                continue
            for ln in by_r[other["ReceiptID"]]:
                if ln["SKU"] and kept_skus[ln["SKU"]] > 0:
                    kept_skus[ln["SKU"]] -= 1
                    actions.append({"op": "delete_line", "table": "ReceiptItems", "row_id": ln["ItemID"],
                                    "changes": [("*", _line_text(ln), None)], "reason": "duplicate_receipt",
                                    "why": f"receipt {other['ReceiptID']} copies {keep['ReceiptID']} "
                                           f"({number}); SKU {ln['SKU']} is already on {keep['ReceiptID']}"})
                    del lines[ln["ItemID"]]
                else:
                    actions.append({"op": "move_line", "table": "ReceiptItems", "row_id": ln["ItemID"],
                                    "changes": [("ReceiptID", other["ReceiptID"], keep["ReceiptID"]),
                                                ("LineIndex", ln["LineIndex"], next_index)],
                                    "reason": "duplicate_receipt",
                                    "why": f"receipt {other['ReceiptID']} copies {keep['ReceiptID']} "
                                           f"({number}); this line isn't on {keep['ReceiptID']}"})
                    ln["ReceiptID"], ln["LineIndex"] = keep["ReceiptID"], next_index
                    next_index += 1
            actions.append({"op": "delete_receipt", "table": "Receipts", "row_id": other["ReceiptID"],
                            "changes": [("*", _receipt_text(other, len(by_r[other["ReceiptID"]])), None)],
                            "reason": "duplicate_receipt",
                            "why": f"same receipt number as {keep['ReceiptID']}, which has more lines "
                                   f"({len(by_r[keep['ReceiptID']])}); kept receipt: "
                                   f"{_receipt_text(keep, len(by_r[keep['ReceiptID']]))}"})
            del receipts[other["ReceiptID"]]


def _batches(rlines):
    """Import batches: by ItemID, a new batch starts where LineIndex restarts."""
    out, prev = [], None
    for ln in sorted(rlines, key=lambda ln: ln["ItemID"]):
        if prev is None or ln["LineIndex"] <= prev:
            out.append([])
        out[-1].append(ln)
        prev = ln["LineIndex"]
    return out


def _key(ln):
    return (ln["SKU"], ln["Description"], ln["Price"])


def plan_duplicate_lines(receipts, lines, actions, flags):
    for rid, rlines in sorted(_by_receipt(lines).items()):
        if len({ln["LineIndex"] for ln in rlines}) == len(rlines):
            continue
        batches = _batches(rlines)
        biggest = max(batches, key=lambda b: (len(b), b[-1]["ItemID"]))
        have = Counter(_key(ln) for ln in biggest)
        planned = []
        for b in batches:
            if b is biggest:
                continue
            if Counter(_key(ln) for ln in b) - have:
                flags.append({"receipt": rid, "line": None, "reason": "duplicate_lines",
                              "note": f"import batch of {len(b)} line(s) (ItemID {b[0]['ItemID']}-"
                                      f"{b[-1]['ItemID']}) has lines that aren't in the biggest batch "
                                      f"({len(biggest)} lines); a person decides"})
                planned = None
                break
            planned += b
        if planned is None:
            continue
        for ln in planned:
            actions.append({"op": "delete_line", "table": "ReceiptItems", "row_id": ln["ItemID"],
                            "changes": [("*", _line_text(ln), None)], "reason": "duplicate_lines",
                            "why": f"receipt {rid} was imported {len(batches)} times; every line of this "
                                   f"earlier batch is also in the biggest one (ItemID "
                                   f"{biggest[0]['ItemID']}-{biggest[-1]['ItemID']}, {len(biggest)} lines)"})
            del lines[ln["ItemID"]]
        if len({ln["LineIndex"] for ln in biggest}) != len(biggest):
            flags.append({"receipt": rid, "line": None, "reason": "duplicate_lines",
                          "note": "line numbers still repeat inside the kept batch"})


def _raw_text(raw_dir: Path, receipt) -> str | None:
    for fid in (receipt["RawFileID"], receipt["SourceFile"]):
        if fid and re.fullmatch(r"[\w.-]+", fid):
            p = Path(raw_dir) / f"{fid}.txt"
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
    return None


def raw_prices_after_sku(raw: str, sku: str) -> list[Decimal]:
    """Prices written after `sku` in the raw text, up to the next SKU."""
    found = []
    for m in re.finditer(re.escape(sku), raw):
        seg = _SKU_RE.split(raw[m.end():m.end() + 120])[0]
        found += [Decimal(f"{a}.{b}") for a, b in _PRICE_RE.findall(seg)]
    return found


def plan_decimal_loss(receipts, lines, actions, flags, raw_dir):
    by_r = _by_receipt(lines)
    for rid, rlines in sorted(by_r.items()):
        suspects = [ln for ln in rlines if ln["Price"] is not None and ln["Price"] > 500]
        if not suspects:
            continue
        rec = receipts[rid]
        cands = [ln for ln in suspects if ln["PriceOriginal"] is None
                 and ln["Price"] / 100 <= 500 and (ln["Price"] / 100) == (ln["Price"] / 100).quantize(CENT)]
        for ln in suspects:
            if ln not in cands:
                flags.append({"receipt": rid, "line": ln["ItemID"], "reason": "decimal_loss",
                              "note": f"{_line_text(ln)}: not a decimal-loss candidate (price / 100 isn't a "
                                      "whole-cent price under $500)"})
        if not cands:
            continue
        total = sum((ln["Price"] for ln in rlines if ln["Price"] is not None), Decimal(0))
        fixed_sum = total - sum((ln["Price"] - ln["Price"] / 100 for ln in cands), Decimal(0))
        sub = rec["Subtotal"]
        evidence_b = sub is not None and abs(fixed_sum - sub) <= Decimal("0.05")
        raw = _raw_text(raw_dir, rec)
        for ln in cands:
            new = (ln["Price"] / 100).quantize(CENT)
            shown = raw_prices_after_sku(raw, ln["SKU"]) if raw and ln["SKU"] else []
            evidence_a = new in shown
            why = []
            if evidence_a:
                why.append(f"(a) the raw text shows {new} after SKU {ln['SKU']}")
            if evidence_b:
                why.append(f"(b) the line sum becomes {fixed_sum}, subtotal {sub}")
            if why:
                actions.append({"op": "update_price", "table": "ReceiptItems", "row_id": ln["ItemID"],
                                "changes": [("Price", ln["Price"], new)], "reason": "decimal_loss",
                                "why": f"receipt {rid}, {_line_text(ln)}: " + "; ".join(why)})
                ln["PriceOriginal"], ln["Price"] = ln["Price"], new
            else:
                seen = ("no raw text" if raw is None else
                        ("raw shows " + ", ".join(str(p) for p in shown)) if shown else "raw shows no price")
                flags.append({"receipt": rid, "line": ln["ItemID"], "reason": "decimal_loss",
                              "note": f"{_line_text(ln)} -> {new}? no evidence: {seen}; line sum would be "
                                      f"{fixed_sum}, subtotal {sub}"})


def plan(con, raw_dir):
    receipts, lines = load(con)
    actions, flags = [], []
    plan_duplicate_receipts(receipts, lines, actions, flags)
    plan_duplicate_lines(receipts, lines, actions, flags)
    plan_decimal_loss(receipts, lines, actions, flags, raw_dir)
    remaining_numbers = [n for n, c in Counter(r["ReceiptNumber"] for r in receipts.values()
                                               if r["ReceiptNumber"] is not None).items() if c > 1]
    remaining_lines = [k for k, c in Counter((ln["ReceiptID"], ln["LineIndex"])
                                             for ln in lines.values()).items() if c > 1]
    return actions, flags, {"receipt_numbers": sorted(remaining_numbers), "lines": sorted(remaining_lines)}


# --- apply -----------------------------------------------------------------------

def _fix_rows(action, fixed_by):
    return [(action["table"], action["row_id"], col, None if old is None else str(old)[:200],
             None if new is None else str(new)[:200], action["reason"], fixed_by)
            for col, old, new in action["changes"]]


def apply(con, actions, fixed_by) -> None:
    """One transaction. Every statement must hit exactly the planned row in
    its planned state, or nothing is changed."""
    cur = con.cursor()
    try:
        for a in actions:
            if a["op"] == "delete_line":
                cur.execute("DELETE FROM dbo.ReceiptItems WHERE ItemID = ?", a["row_id"])
            elif a["op"] == "move_line":
                (_, r_old, r_new), (_, l_old, l_new) = a["changes"]
                cur.execute("UPDATE dbo.ReceiptItems SET ReceiptID = ?, LineIndex = ? "
                            "WHERE ItemID = ? AND ReceiptID = ? AND LineIndex = ?",
                            r_new, l_new, a["row_id"], r_old, l_old)
            elif a["op"] == "delete_receipt":
                cur.execute("DELETE FROM dbo.Receipts WHERE ReceiptID = ? AND NOT EXISTS "
                            "(SELECT 1 FROM dbo.ReceiptItems WHERE ReceiptID = ?)", a["row_id"], a["row_id"])
            elif a["op"] == "update_price":
                (_, old, new), = a["changes"]
                cur.execute("UPDATE dbo.ReceiptItems SET Price = ?, PriceOriginal = ? "
                            "WHERE ItemID = ? AND Price = ? AND PriceOriginal IS NULL",
                            new, old, a["row_id"], old)
            else:
                raise ValueError(a["op"])
            if cur.rowcount != 1:
                raise RuntimeError(f"{a['op']} {a['table']} {a['row_id']}: the row changed since it was "
                                   "planned; nothing was applied. Run the dry run again.")
            cur.executemany("INSERT INTO dbo.receipt_fixes (table_name, row_id, column_name, old_value, "
                            "new_value, reason, fixed_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            _fix_rows(a, fixed_by))
        con.commit()
    except BaseException:
        con.rollback()
        raise


# --- report ----------------------------------------------------------------------

def write_report(path, *, target, applied, actions, flags, remaining, raw_dir):
    out = []
    w = out.append
    w(f"# Receipts cleanup: {'APPLIED' if applied else 'DRY RUN'} on `{target}`\n")
    w(f"- When: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    w(f"- Raw receipt texts: `{raw_dir}`")
    by = Counter((a["reason"], a["op"]) for a in actions)
    w(f"- Changes: {len(actions)} (" + ", ".join(f"{op} {n} [{r}]" for (r, op), n in sorted(by.items())) + ")")
    w(f"- Flagged for a person, no change: {len(flags)}")
    dups = len(remaining["receipt_numbers"]) + len(remaining["lines"])
    w(f"- Duplicates left {'after' if applied else 'after applying'}: "
      f"{'none, the unique indexes (step 2) can be built' if not dups else remaining}\n")
    w("## Changes\n")
    w("| # | Reason | Change | Row | Column: old → new | Why |\n|---:|---|---|---|---|---|")
    for i, a in enumerate(actions, 1):
        ch = "; ".join(f"{c}: {o} → {n if n is not None else '(deleted)'}" for c, o, n in a["changes"])
        w(f"| {i} | {a['reason']} | {a['op']} | {a['table']} {a['row_id']} | {ch} | {a['why']} |")
    w("\n## Flagged for a person (no change)\n")
    w("| Receipt | Line (ItemID) | Reason | Note |\n|---:|---:|---|---|")
    for f in flags:
        w(f"| {f['receipt']} | {f['line'] or ''} | {f['reason']} | {f['note']} |")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", required=True)
    ap.add_argument("--production", action="store_true", help="allow --target ResaleDB")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    ap.add_argument("--report-dir", default=str(config.LOG_DIR))
    ap.add_argument("--fixed-by", default="receipts_cleanup")
    args = ap.parse_args(argv)
    _mig.check_target(args.target, args.production)

    con = sqlserver.connect(args.target)
    try:
        actions, flags, remaining = plan(con, Path(args.raw_dir))
        con.rollback()
        if args.apply and actions:
            apply(con, actions, args.fixed_by[:30])
            remaining = migrations.mssql_receipt_duplicates(con.cursor())
            con.commit()
    finally:
        con.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path(args.report_dir) / f"receipts-cleanup-{args.target}-{stamp}{'' if args.apply else '-dry-run'}.md"
    write_report(path, target=args.target, applied=args.apply, actions=actions, flags=flags,
                 remaining=remaining, raw_dir=args.raw_dir)
    by = Counter(a["reason"] for a in actions)
    print(f"{'APPLIED' if args.apply else 'DRY RUN (nothing changed; --apply to make these changes)'}: "
          f"{len(actions)} change(s) " + str(dict(by)) + f", {len(flags)} flagged for a person")
    for i, a in enumerate(actions, 1):
        print(f"  {i:3}. [{a['reason']}] {a['op']} {a['table']} {a['row_id']}: "
              + "; ".join(f"{c} {o} -> {n}" for c, o, n in a["changes"]))
    left = remaining["receipt_numbers"] or remaining["lines"]
    print("Duplicates left:", "none" if not left else remaining)
    print(f"Report: {path}")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
