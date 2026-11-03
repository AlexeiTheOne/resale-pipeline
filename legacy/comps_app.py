"""
comps_app.py - small local web page for the Ross comps check.

Put this file in the project root next to comps.py, then run:

    python comps_app.py

It opens http://127.0.0.1:8765 in your browser. Type an item description, press
Search, and everything shows on the same page: sold + active listings (with links),
units sold, best sellers, and "The math" - a live profit calculator you can change
(sale price, Ross price, shipping label) without searching again.

Nothing to install: it uses only Python's standard library plus your existing
comps.py, pipeline/price.py, config.py and .env. It listens on 127.0.0.1 only, so it
is reachable from this PC and nowhere else. Stop it with Ctrl+C.
"""

import sys
import json
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import comps

HOST, PORT = "127.0.0.1", 8765


# ---------------------------------------------------------------- request handling
def _words(text) -> list[str]:
    return [w for w in str(text or "").replace(",", " ").split() if w]


def _num(v):
    try:
        return float(str(v).replace("$", "").strip()) if str(v).strip() else None
    except ValueError:
        return None


def run_search(payload: dict) -> dict:
    description = str(payload.get("description") or "").strip()
    if not description:
        raise ValueError("Type an item description first.")
    require = _words(payload.get("require")) or None
    size = _words(payload.get("size")) or None
    exclude = _words(payload.get("exclude")) or None
    brand = str(payload.get("brand") or "").strip() or None
    started = time.time()
    res = comps.get_comps(
        description, require, size, brand, None,
        _num(payload.get("cost")), _num(payload.get("ship")),
        bool(payload.get("womens")), details=bool(payload.get("details", True)),
        exclude=exclude,
    )
    res["elapsed_seconds"] = round(time.time() - started, 1)
    return res


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(204, b"", "text/plain")

    def do_POST(self):
        if self.path != "/api/comps":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, run_search(payload))
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except SystemExit as e:          # e.g. missing keys in .env
            self._json(500, {"error": str(e) or "stopped"})
        except Exception as e:
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            print(f"  {self.address_string()} {args[0]} -> {args[1]}")


def main():
    server = None
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"Ports {PORT}-{PORT + 9} are all busy. Close the old window and retry.")
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"Comps app running at {url}   (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- the page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ross comps check</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#6b7280;--card:#f6f7f9;--line:#dde0e5;--accent:#2563eb;
--good:#15803d;--warn:#b45309;--bad:#b91c1c;--goodbg:#dcfce7;--warnbg:#fef3c7;--badbg:#fee2e2}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8e8;--muted:#9aa0a6;--card:#1d2026;
--line:#2d323a;--accent:#60a5fa;--good:#4ade80;--warn:#fbbf24;--bad:#f87171;--goodbg:#12301e;--warnbg:#38290c;--badbg:#3b1616}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,"Segoe UI",Arial,sans-serif}
main{max-width:1120px;margin:0 auto;padding:18px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:2px}
input[type=text],input[type=number]{width:100%;padding:8px 9px;border:1px solid var(--line);border-radius:7px;
background:var(--bg);color:var(--fg);font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:0}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:10px}
.row label{display:inline-flex;gap:6px;align-items:center;margin:0;font-size:13px;color:var(--fg)}
button{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--accent);padding:4px 10px;font-weight:500;font-size:13px}
.muted{color:var(--muted);font-size:13px}
.banner{border-radius:10px;padding:14px;margin-bottom:14px;border:1px solid var(--line)}
.banner b.v{font-size:22px}
.good{background:var(--goodbg);color:var(--good)}.warn{background:var(--warnbg);color:var(--warn)}.bad{background:var(--badbg);color:var(--bad)}
.tiles{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.tile{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px}
.tile .big{font-size:20px;font-weight:700}
.mathgrid{display:grid;gap:18px;grid-template-columns:minmax(230px,300px) 1fr}
@media (max-width:760px){.mathgrid{grid-template-columns:1fr}}
.mathgrid label{margin-top:8px}
table{border-collapse:collapse;width:100%}
td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td.n,th.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
tr.total td{font-weight:700;border-top:2px solid var(--line)}
.bar{display:flex;height:16px;border-radius:5px;overflow:hidden;margin:10px 0 4px;background:var(--line)}
.bar span{display:block;height:100%}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
.scroll{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.tag{font-size:11px;color:var(--warn);margin-left:6px}.tag.ok{color:var(--good)}.tag.muted{color:var(--muted)}
a{color:var(--accent)}
.err{background:var(--badbg);color:var(--bad);padding:12px;border-radius:8px}
ul.w{margin:6px 0 0;padding-left:20px}
details summary{cursor:pointer;color:var(--accent);margin-top:10px}
</style></head>
<body><main>
<h1>Ross comps check</h1>

<form id="form" class="card" autocomplete="off">
  <label for="description">Item description</label>
  <input type="text" id="description" placeholder="e.g. Burberry London for Men eau de toilette" required autofocus>
  <div class="grid" style="margin-top:10px">
    <div><label for="cost">Ross price ($, before tax)</label><input type="number" id="cost" step="0.01" min="0" placeholder="10.99"></div>
    <div><label for="ship">Shipping label you pay ($): bags/backpacks ~10-14</label><input type="number" id="ship" step="0.01" min="0" placeholder="default from config"></div>
    <div><label for="size">Size (any of)</label><input type="text" id="size" placeholder="1.6   or   9"></div>
    <div><label for="brand">Brand must be in title</label><input type="text" id="brand" placeholder="Burberry"></div>
    <div><label for="require">Must contain</label><input type="text" id="require" placeholder="london"></div>
    <div><label for="exclude">Exclude words</label><input type="text" id="exclude" placeholder="weekend fabric"></div>
  </div>
  <div class="row">
    <label><input type="checkbox" id="details" checked> Look up units sold per listing (a few seconds slower)</label>
    <label><input type="checkbox" id="womens"> Women's sizes</label>
    <button id="go" type="submit">Search</button>
    <span id="status" class="muted"></span>
  </div>
</form>

<div id="out"></div>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl = u => (typeof u === "string" && /^https?:\/\//i.test(u)) ? u : "";
const money = v => (v === null || v === undefined || isNaN(v)) ? "-" : (v < 0 ? "-$" + Math.abs(v).toFixed(2) : "$" + Number(v).toFixed(2));
const num = id => { const v = parseFloat($(id).value); return isNaN(v) ? null : v; };
let R = null;

// ---------- math (same formulas as comps.py) ----------
function calc(price, cost, label) {
  const a = R.assumptions;
  const fvf = price * a.fvf_pct, ad = price * a.ad_pct, fixed = a.fixed_fee;
  const fees = fvf + ad + fixed;
  const keep = price - fees - label;
  const costTax = cost * (1 + a.sales_tax);
  const profit = keep - costTax;
  const be = (costTax + label + fixed) / (1 - a.fvf_pct - a.ad_pct);
  return {fvf, ad, fixed, fees, keep, costTax, profit, roi: costTax > 0 ? profit / costTax : null, be};
}
function verdictFor(profit, roi, tier) {
  if (profit >= 10 && roi >= 0.5)
    return tier === "solid" ? ["BUY", "good"] : ["MAYBE - no solid sold data, check by hand", "warn"];
  if (profit >= 0)
    return ["BORDERLINE - bundle or skip" + (tier === "solid" ? "" : ", and no solid sold data"), "warn"];
  return ["SKIP", "bad"];
}

// ---------- search ----------
// keep the calculator in step with the top form
$("cost").addEventListener("input", () => { if (R && $("m_cost")) { $("m_cost").value = $("cost").value; updateMath(); } });
$("ship").addEventListener("input", () => {
  if (R && $("m_label")) { $("m_label").value = $("ship").value !== "" ? $("ship").value : Number(R.assumptions.ship_cost).toFixed(2); updateMath(); } });

$("form").addEventListener("submit", async e => {
  e.preventDefault();
  const body = {
    description: $("description").value, size: $("size").value, brand: $("brand").value,
    require: $("require").value, exclude: $("exclude").value, cost: $("cost").value,
    ship: $("ship").value, womens: $("womens").checked, details: $("details").checked
  };
  $("go").disabled = true; $("out").innerHTML = "";
  const t0 = Date.now();
  const tick = setInterval(() => { $("status").textContent = "Searching... " + Math.round((Date.now() - t0) / 1000) + "s"; }, 500);
  try {
    const resp = await fetch("/api/comps", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || ("HTTP " + resp.status));
    R = data; render();
    $("status").textContent = "Done in " + data.elapsed_seconds + "s";
  } catch (err) {
    $("out").innerHTML = '<div class="err"><b>Search failed:</b> ' + esc(err.message) + "</div>";
    $("status").textContent = "";
  } finally { clearInterval(tick); $("go").disabled = false; }
});

// ---------- render ----------
function render() {
  const s = R.stats, a = R.assumptions;
  const best = R.active.filter(x => x.sold_qty).sort((x, y) => y.sold_qty - x.sold_qty).slice(0, 5);
  const presets = [];
  if (s.evidence !== "none" && s.sold_median) presets.push(["Sold median", s.sold_median]);
  if (s.units_sold_weighted_price) presets.push(["Units-sold-weighted", s.units_sold_weighted_price]);
  if (a.active_p25) presets.push(["Active 25th pct", a.active_p25]);
  if (s.active_lowest) presets.push(["Lowest active", s.active_lowest]);
  if (s.active_median) presets.push(["Active median", s.active_median]);

  $("out").innerHTML = `
  <div id="banner" class="banner"></div>

  <div class="card"><h2>The math</h2>
    <div class="mathgrid">
      <div>
        <label>Sale price: what the buyer pays IN TOTAL (item + shipping)</label>
        <input type="number" id="m_price" step="0.01" min="0">
        <div id="presets" class="row" style="margin-top:6px">${presets.map(p =>
          `<button type="button" class="ghost" data-p="${p[1]}" title="${esc(p[0])}">${esc(p[0])} ${money(p[1])}</button>`).join("")}</div>
        <label>Your Ross price (before tax)</label><input type="number" id="m_cost" step="0.01" min="0">
        <label>Shipping label you pay the carrier</label><input type="number" id="m_label" step="0.01" min="0">
        <label>Shipping you charge the buyer (0 = free shipping)</label><input type="number" id="m_charged" step="0.01" min="0" value="0">
        <p id="m_list" class="muted"></p>
      </div>
      <div><table id="m_table"></table>
        <div id="m_bar" class="bar"></div><div id="m_legend" class="legend"></div></div>
    </div>
    <details><summary>How this works</summary>
      <p class="muted">All prices on this page are what the buyer pays in total, item + shipping.
      Shipping the buyer pays is already inside that number; the label is what you pay the carrier, so it comes out of it.
      eBay's fees are charged on the total, so listing at $27.99 + $7 shipping or $34.99 free shipping gives the same profit.
      Fee rates, ad rate and per-order fee come from config.py (measured on your real orders). Ross tax is ${(a.sales_tax * 100).toFixed(0)}%.
      Break-even is the sale price where profit is exactly $0.</p></details>
  </div>

  <div class="tiles" style="margin-bottom:14px">
    <div class="tile"><div class="muted">Sold, last 90 days</div><div class="big">${s.sold_count_90d} matched</div>
      <div>median ${money(s.sold_median)} &middot; range ${money(s.sold_p10)}&ndash;${money(s.sold_p90)}</div>
      <div class="muted">evidence: ${esc(s.evidence)} (${esc(s.match_level_sold)} match)</div></div>
    <div class="tile"><div class="muted">Active now</div><div class="big">${s.active_count} matched</div>
      <div>lowest ${money(s.active_lowest)} &middot; median ${money(s.active_median)}</div>
      <div class="muted">${s.has_codes ? s.active_code_matches + " of " + s.active_count + " contain the exact style code &middot; " : ""}${R.debug.active_ebay_total} results on eBay for this search${s.ship_unknown ? " &middot; " + s.ship_unknown + " without a shipping cost" : ""}</div></div>
    <div class="tile"><div class="muted">Units sold by active listings (lifetime)</div>
      <div class="big">${s.active_units_sold_total ?? "-"}</div>
      <div>${s.listings_with_sales} of ${s.active_count} listings have sold</div>
      <div class="muted">weighted price ${money(s.units_sold_weighted_price)} (multi-size excluded, 10-unit cap)</div></div>
  </div>

  ${R.warnings.length ? `<div class="card"><b>Watch out</b><ul class="w">${R.warnings.map(w => "<li>" + esc(w) + "</li>").join("")}</ul></div>` : ""}

  ${best.length ? `<div class="card"><h2>Best sellers among active listings</h2><table>${best.map(b =>
    `<tr><td class="n">${b.sold_qty} sold</td><td class="n">${money(b.price)}</td><td>${linkTitle(b)}</td></tr>`).join("")}</table></div>` : ""}

  <div class="card"><h2>Sold listings (${R.sold.length})</h2>
    <div class="scroll"><table><tr><th class="n">Price</th><th>Listing</th></tr>${
      R.sold.map(x => `<tr><td class="n">${money(x.price)}</td><td>${linkTitle(x)}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">No matching sales found.</td></tr>'
    }</table></div></div>

  <div class="card"><h2>Active listings (${R.active.length}), cheapest first</h2>
    <label style="display:inline-flex;gap:6px;align-items:center;color:var(--fg);font-size:13px"><input type="checkbox" id="onlySold"> Only listings that have sold</label>
    <div class="scroll" style="margin-top:8px"><table id="activeTable"></table></div></div>

  <div class="card muted">Open on eBay: ${Object.entries(R.links).map(([k, u]) =>
    `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(k.replace(/_/g, " "))}</a>`).join(" &middot; ")}
    <br>Searched for: ${esc(R.query)} &middot; shipping priced for a buyer in zip ${esc(R.debug.buyer_zip)}
    <br>Apify: ${R.debug.apify_calls.length} call(s), ${R.debug.apify_rows_billed} rows billed
    &middot; eBay lookups: ${R.debug.detail_lookups_ok} ok, ${R.debug.detail_lookups_failed} failed</div>`;

  // defaults for the calculator
  const refPrice = R.reference && R.reference.price;
  $("m_price").value = refPrice ? Number(refPrice).toFixed(2) : "";
  $("m_cost").value = $("cost").value;
  $("m_label").value = $("ship").value !== "" ? $("ship").value : Number(a.ship_cost).toFixed(2);
  ["m_price", "m_cost", "m_label", "m_charged"].forEach(id => $(id).addEventListener("input", updateMath));
  document.querySelectorAll("#presets button").forEach(b => b.addEventListener("click", () => {
    $("m_price").value = Number(b.dataset.p).toFixed(2); updateMath(); }));
  $("onlySold").addEventListener("change", drawActive);
  drawActive(); updateMath();
}

function linkTitle(x) {
  const u = safeUrl(x.url), t = esc(x.title) + (x.multi_size ? '<span class="tag">multi-size</span>' : "")
    + (x.code_match ? '<span class="tag ok">exact code</span>' : "")
    + (x.ship_known === undefined ? "" : x.ship_known
        ? (x.shipping > 0 ? `<span class="tag muted">incl. ${money(x.shipping)} shipping</span>` : '<span class="tag muted">free shipping</span>')
        : '<span class="tag">shipping unknown</span>');
  return u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${t}</a>` : t;
}

function drawActive() {
  const only = $("onlySold").checked;
  const rows = R.active.filter(x => !only || x.sold_qty > 0);
  $("activeTable").innerHTML = '<tr><th class="n">Price</th><th class="n">Sold</th><th>Listing</th></tr>' +
    (rows.map(x => `<tr><td class="n">${money(x.price)}</td><td class="n">${x.sold_qty ?? "-"}</td><td>${linkTitle(x)}</td></tr>`).join("")
     || '<tr><td colspan="3" class="muted">Nothing to show.</td></tr>');
}

function updateMath() {
  const price = num("m_price"), cost = num("m_cost"), label = num("m_label") ?? 0, charged = num("m_charged") ?? 0;
  const banner = $("banner"), tbl = $("m_table");
  if (price === null) { banner.className = "banner"; banner.textContent = "Enter a sale price to see the math."; tbl.innerHTML = ""; return; }
  const c = calc(price, cost ?? 0, label), a = R.assumptions;
  const item = Math.max(0, price - charged);
  $("m_list").innerHTML = charged > 0
    ? `List it at <b>${money(item)}</b> + <b>${money(charged)}</b> shipping (buyer pays ${money(price)}).`
    : `List it at <b>${money(price)}</b> with free shipping.`;
  const pct = v => (price > 0 ? " (" + (v / price * 100).toFixed(1) + "%)" : "");
  const rows = [
    ["Buyer pays (item + shipping)", price, ""],
    [`- eBay final value fee ${(a.fvf_pct * 100).toFixed(1)}%`, -c.fvf, pct(c.fvf)],
    [`- Promoted listing ads ${(a.ad_pct * 100).toFixed(1)}%`, -c.ad, pct(c.ad)],
    ["- eBay per-order fee", -c.fixed, ""],
    ["- Shipping label you pay", -label, pct(label)],
  ];
  let html = rows.map(r => `<tr><td>${r[0]}</td><td class="n">${money(r[1])}</td><td class="muted">${r[2]}</td></tr>`).join("");
  html += `<tr class="total"><td>= You keep from the sale</td><td class="n">${money(c.keep)}</td><td class="muted">${pct(c.keep)}</td></tr>`;
  if (cost === null) {
    html += `<tr><td colspan="3" class="muted">Enter your Ross price to see profit.</td></tr>`;
    banner.className = "banner"; banner.innerHTML = `Enter your Ross price to get a verdict. Break-even needs it too.`;
  } else {
    html += `<tr><td>- Item cost + ${(a.sales_tax * 100).toFixed(0)}% tax (${money(cost)} x ${(1 + a.sales_tax).toFixed(2)})</td><td class="n">${money(-c.costTax)}</td><td class="muted">${pct(c.costTax)}</td></tr>`;
    html += `<tr class="total"><td>= Profit</td><td class="n">${money(c.profit)}</td><td class="muted">ROI ${c.roi === null ? "-" : Math.round(c.roi * 100) + "%"}</td></tr>`;
    html += `<tr><td class="muted">Break-even sale price</td><td class="n">${money(c.be)}</td><td></td></tr>`;
    const [text, cls] = verdictFor(c.profit, c.roi ?? 0, R.stats.evidence);
    banner.className = "banner " + cls;
    banner.innerHTML = `<b class="v">${esc(text)}</b><br>At ${money(price)}: profit <b>${money(c.profit)}</b> (ROI ${c.roi === null ? "-" : Math.round(c.roi * 100) + "%"}) &middot; break-even ${money(c.be)}` +
      `<br><span class="muted">Price basis: ${esc(price === (R.reference && R.reference.price) ? (R.reference.label || "") : "your number")}</span>`;
  }
  tbl.innerHTML = html;
  // where the money goes
  const parts = [["eBay fees + ads", c.fees, "#f59e0b"], ["Shipping label", label, "#6366f1"], ["Item + tax", c.costTax, "#64748b"], ["Profit", Math.max(0, c.profit), "#16a34a"]];
  const total = Math.max(price, parts.reduce((t, p) => t + p[1], 0)) || 1;
  $("m_bar").innerHTML = parts.map(p => `<span style="width:${p[1] / total * 100}%;background:${p[2]}" title="${p[0]} ${money(p[1])}"></span>`).join("");
  $("m_legend").innerHTML = parts.map(p => `<span><i style="background:${p[2]}"></i>${p[0]} ${money(p[1])}</span>`).join("");
}
</script></body></html>
"""

if __name__ == "__main__":
    main()