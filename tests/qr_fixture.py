"""A small eBay listing quality report xlsx, laid out like the real one
(Seller Hub, Nov 2026): same rows and labels, columns found by label. The
real report isn't in git; its figures here are the Pillowcases and Women's
Bags ones from the 11/06/2026 report."""
import openpyxl

GENERATED = "Report generated: US - 11/06/2026 06-38 PST"

BAGS = {
    "sheet": "Womens Bags & Handbags", "category": "Women's Bags & Handbags", "filters": "Listings condition: New",
    "gmv": "1,397 out of 444,519", "sellers": "444,519",
    "funnel": {"impressions": (163, 731, "● Add free shipping to 81% of your listings", (0, 0.81, 0.6077)),
               "ctr": (0.0038, 12686, None, None), "conversion": (0.0104, 3014, None, None)},
    "bench": {"impressions": (163, 499, 2), "ctr": (0.0038, 0.0389, 0.0013), "conversion": (0.0104, 0.3269, 0.0057)},
    # Free shipping before the specifics columns, as in the real bags sheet.
    "columns": ["Item title", "Action", "Daily impressions per listing", "Click-through rate",
                "Sales conversion rate", "Free shipping", "Recommended item specifics filled",
                "Recommended item specifics to add", "Promoted listings ad rate", "Item Id", "Price",
                "Shipping fee", "Item age in days", "Sales count in last 31 days", "Custom label",
                "Number of watchers"],
    "listings": [
        ["Ed Hardy Tiger Hobo Bag", "Edit", 30, 0.0012, "No sales", "Not provided", 15, " ", 0.04,
         117262064558, 69.99, 9.99, 138, "0", "bag00001-aaaa", "2"],
        ["DKNY Astrid Satchel", "Edit", 240, 0.0050, 0.02, "Not provided", 14, " ", 0.04,
         117275668558, 63.99, 9.99, 130, 1, "bag00002-bbbb", "1"],
    ],
}

PILLOWCASES = {
    "sheet": "Pillowcases", "category": "Pillowcases", "filters": "Listings condition: New, Price range: $35 - $11,405",
    "gmv": "255 out of 7,296", "sellers": "7,296",
    "funnel": {"impressions": (79, 367, "● Consider increasing promoted listings ad rate to 7%",
                               (0.04, 0.07, 0.0567)),
               "ctr": (0.0048, 1995, None, None), "conversion": (0.0228, 262, None, None)},
    "bench": {"impressions": (79, 304, 1), "ctr": (0.0048, 0.0695, 0.001), "conversion": (0.0228, 1.6931, 0.013)},
    "columns": ["Item title", "Action", "Daily impressions per listing", "Click-through rate",
                "Sales conversion rate", "Promoted listings ad rate", "Recommended item specifics filled",
                "Recommended item specifics to add", "Free shipping", "Item Id", "Price", "Shipping fee",
                "Item age in days", "Sales count in last 31 days", "Custom label", "Number of watchers"],
    "listings": [
        ["RL Estella Trellis King Pillowcases", "Edit", 63, "No clicks", "No sales", 0.04, 8, " ", "Not provided",
         117306549500, 39.99, 9.99, 113, "0", "pil00001-cccc", "0"],
    ],
}

_FUNNEL_ROWS = {"impressions": (9, "       Impressions in eBay search", "Average per day"),
                "ctr": (13, "       Click-through", "Rate"),
                "conversion": (17, "       Sales conversion", "Conversion")}
_BENCH_ROWS = {"impressions": (24, "Impressions in eBay search", "Daily impressions per listing"),
               "ctr": (28, "Click-through", "Click-through rate"),
               "conversion": (32, "Sales conversion", "Sales conversion rate")}
_BENCH_EXTRA = ["Recommended item specifics filled", "Brand", "Number of photos"]


def _section(ws, sec, generated=GENERATED):
    ws.cell(2, 2, "We compared your listings to similar listings in ebay with the properties below")
    ws.cell(2, 10, f"Your sales rank (GMV) in this section: {sec['gmv']} sellers")
    ws.cell(3, 2, f"Category: {sec['category']}, {sec['filters']}")
    ws.cell(3, 10, generated)
    ws.cell(6, 2, "Buyer funnel")
    ws.cell(7, 2, f"Sellers selling in this section: {sec['sellers']}")
    ws.cell(7, 8, "Recommendations to improve")
    ws.cell(9, 11, "Your listings")
    for stage, (row, title, label) in _FUNNEL_ROWS.items():
        value, rank, rec, vals = sec["funnel"][stage]
        ws.cell(row, 4, title)
        ws.cell(row + 1, 4, label)
        ws.cell(row + 1, 6, "Rank vs other sellers")
        ws.cell(row + 1, 8, rec or "Your listings' quality meets the top benchmark")
        if vals:
            for c, v in zip((11, 12, 13), vals):
                ws.cell(row + 1, c, v)
        ws.cell(row + 2, 4, value)
        ws.cell(row + 2, 6, rank)
    ws.cell(22, 2, "More benchmarks")
    for stage, (row, label, metric) in _BENCH_ROWS.items():
        ws.cell(row, 2, label)
        ws.cell(row, 6, metric)
        if stage == "impressions":  # only the first block labels the other columns
            for k, h in enumerate(_BENCH_EXTRA):
                ws.cell(row, 7 + k, h)
        ours, top, bottom = sec["bench"][stage]
        for k, (who, v) in enumerate((("premium_designer_outlet", ours),
                                      ("Top 10% of items per seller", top),
                                      ("Bottom 10% of items per seller", bottom)), 1):
            ws.cell(row + k, 2, who)
            ws.cell(row + k, 6, v)
            ws.cell(row + k, 7, 8)
            ws.cell(row + k, 8, "Not relevant" if k > 1 else 1)
            ws.cell(row + k, 9, 6.3)
    ws.cell(37, 2, "* Analysis based on last 31 days, only 'Buy it now' listings")
    for c, h in enumerate(sec["columns"]):
        ws.cell(44, 2 + c, h)
    for r, values in enumerate(sec["listings"]):
        for c, v in enumerate(values):
            ws.cell(45 + r, 2 + c, v)


def write_report(path, sections=(BAGS, PILLOWCASES), generated=GENERATED):
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.cell(2, 2, "Listing quality summary: Category overview BETA")
    summary.cell(2, 5, generated)
    wb.create_sheet("Guide").cell(2, 2, "Guide")
    for sec in sections:
        _section(wb.create_sheet(sec["sheet"]), sec, generated)
    wb.save(path)
    return path
