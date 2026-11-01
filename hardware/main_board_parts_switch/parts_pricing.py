#!/usr/bin/env python3
"""Web GUI for pricing an order from parts_crossmatch.yaml.

Usage:
    python3 parts_pricing.py [yaml_file] [--state parts_order.json] [--port 8767] [--no-open] [--keep-alive]

Starts a local web server (127.0.0.1 only). Tick the parts you want, set the
quantity (designators x boards by default, with an optional buffer and a minimum
order quantity) and pick the site each part is ordered from. The side panel sums
the price per site and exports one CSV per site.

The YAML is only read, never written. Your choices are auto-saved to a small JSON
file next to it (parts_order.json by default). The server stops by itself when you
close the page (or Ctrl+C); pass --keep-alive to keep it running.

Requires PyYAML.
"""
import argparse
import json
import math
import os
import re
import select
import socket
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from parts_editor import Clients, load

MAX_BODY = 10_000_000
NUM_FIELDS = ("need", "buf", "min")
EMPTY_STATE = {"boards": 1, "parts": {}}


def clean_num(value):
    if value in ("", None):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid number: {value!r}")
    return int(number) if number == int(number) else number


def clean_state(data):
    """Validate the payload from the browser and keep only the known fields."""
    if not isinstance(data, dict):
        raise ValueError("state must be an object")
    boards = clean_num(data.get("boards")) or 1
    parts = {}
    for mpn, c in (data.get("parts") or {}).items():
        out = {}
        if isinstance(c.get("include"), bool):
            out["include"] = c["include"]
        for key in NUM_FIELDS:
            number = clean_num(c.get(key))
            if number is not None:
                out[key] = number
        if c.get("unit") in ("pcs", "%"):
            out["unit"] = c["unit"]
        if re.fullmatch(r"[sr]\d+", str(c.get("src") or "")):
            out["src"] = c["src"]
        if out:
            parts[str(mpn)] = out
    return {"boards": boards, "parts": parts}


def load_state(path):
    try:
        return clean_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError):
        return dict(EMPTY_STATE)


def save_state(path, state):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def make_handler(path, state_path, clients):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _hold_open(self):
            """Keep the connection open until the browser closes it (page closed)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            clients.connect()
            try:
                self.wfile.write(b": connected\n\n")
                while True:
                    readable, _, _ = select.select([self.connection], [], [], 5)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        break  # browser closed the connection
                    if not readable:
                        self.wfile.write(b": keep-alive\n\n")  # raises if the peer is gone
            except OSError:
                pass
            finally:
                clients.disconnect()

        def do_GET(self):
            if self.path == "/api/events":
                return self._hold_open()
            if self.path == "/":
                self._send(200, PAGE.replace("__FILE__", path.name), "text/html")
            elif self.path == "/api/parts":
                with lock:
                    self._send(200, json.dumps(load(path)))
            elif self.path == "/api/state":
                with lock:
                    self._send(200, json.dumps(load_state(state_path)))
            else:
                self._send(404, '{"error":"not found"}')

        def do_PUT(self):
            # Only accept writes from our own page (guards against other sites
            # posting to localhost, and DNS rebinding).
            origin = self.headers.get("Origin")
            if self.path != "/api/state" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                return self._send(403, '{"error":"forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, '{"error":"too large"}')
            try:
                state = clean_state(json.loads(self.rfile.read(length)))
                with lock:
                    save_state(state_path, state)
                self._send(200, json.dumps({"ok": True}))
            except OSError as e:
                self._send(500, json.dumps({"error": f"save failed: {e}"}))
            except (ValueError, AttributeError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}))

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parts pricing</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#1c2026;--mute:#667085;--line:#e1e4ea;--acc:#2563eb;--bad:#b42318;--ok:#12805c}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--panel:#1c2027;--fg:#e6e8ec;--mute:#8b93a1;--line:#2d333d;--acc:#6ea0ff;--bad:#ff8a80;--ok:#4ad19b}}
:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;height:100vh}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0}
header .file{color:var(--mute)}
header .sp{flex:1}
header label{color:var(--mute);display:flex;gap:6px;align-items:center}
header label input{width:60px}
#status{color:var(--mute)}
#status.dirty{color:var(--bad)}
#status.saved{color:var(--ok)}
main{flex:1;display:flex;min-height:0}
aside{width:340px;border-right:1px solid var(--line);background:var(--panel);overflow:auto;padding:14px}
section{flex:1;overflow:auto;display:flex;flex-direction:column;min-width:0}
.tools{display:flex;gap:8px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);align-items:center}
.tools input[type=text],.tools input:not([type]){flex:1;max-width:420px}
input,select,button{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px}
input{width:100%}
input[type=checkbox]{width:auto}
button{cursor:pointer;background:var(--panel)}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
.wrap{overflow:auto;flex:1;padding:0 16px 16px}
table{border-collapse:collapse;width:100%}
th{text-align:left;font-weight:500;color:var(--mute);font-size:12px;padding:8px 6px 4px;position:sticky;top:0;background:var(--bg);z-index:1}
td{padding:6px;vertical-align:top;border-bottom:1px solid var(--line)}
tr.off td{opacity:.45}
tr.off td:first-child{opacity:1}
.m{font-weight:600;word-break:break-all}
.d{color:var(--mute);font-size:12px}
.n{width:70px}
.buf{display:flex;gap:4px}
.buf input{width:56px}
.r{text-align:right;white-space:nowrap}
.qty{font-weight:600}
.src{display:flex;gap:4px;align-items:center}
.src select{min-width:150px;max-width:230px}
.open{text-decoration:none;color:var(--acc);padding:5px 4px}
.warn{color:var(--bad)}
.empty{color:var(--mute);margin:60px auto;text-align:center}
.total{margin-bottom:14px}
.big{font-size:28px;font-weight:600}
.sub{color:var(--mute);font-size:12px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px;background:var(--bg)}
.card .h{display:flex;gap:8px;align-items:baseline}
.card .h b{flex:1}
.card .amt{font-weight:600}
.card button{margin-top:8px;width:100%}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:18px 0 8px}
</style></head><body>
<header>
  <h1>Parts pricing</h1><span class="file">__FILE__</span><span class="sp"></span>
  <label>Boards <input id="boards" type="number" min="1" step="1" value="1"></label>
  <span id="status"></span>
</header>
<main>
  <aside id="summary"></aside>
  <section>
    <div class="tools">
      <input id="q" placeholder="Search MPN, description, designator, site…">
      <select id="filter" aria-label="Filter">
        <option value="all">All parts</option>
        <option value="sel">Selected</option>
        <option value="unsel">Not selected</option>
        <option value="noprice">Selected, missing price or source</option>
      </select>
      <button id="all" title="Select every part currently shown">Select shown</button>
      <button id="none" title="Deselect every part currently shown">Deselect shown</button>
      <button id="reset" title="Forget all quantities, buffers and source choices">Reset</button>
    </div>
    <div class="wrap"><table>
      <thead><tr><th></th><th>Part</th><th>Need</th><th>Buffer</th><th>Min</th><th class="r">Order</th><th>Order from</th><th class="r">Unit €</th><th class="r">Line €</th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
  </section>
</main>
<script>
let parts = [];          // array of {mpn, manufacturer, description, package, designators[], sites[], replacements[]}
let st = {boards:1, parts:{}};   // saved choices: parts[mpn] = {include, need, buf, unit, min, src}
let saveTimer = null;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const filled = v => String(v ?? '').trim() !== '';
const num = v => { if (!filled(v)) return null; const n = Number(v); return Number.isFinite(n) ? n : null; };
const isStock = s => /^on\s*site$/i.test(String(s || '').trim());   // stock you already have: no price expected
const money = n => n.toFixed(2);
const unitPrice = n => n == null ? '—' : String(+n.toFixed(4));
const url = u => /^https?:\/\//.test(u) ? u : '';

// ---- ordering logic -------------------------------------------------------
function options(p){
  const o = [];
  p.sites.forEach((s, j) => filled(s.site) && o.push({key:'s'+j, site:s.site.trim(), price:num(s.price), url:s.url, repl:false}));
  p.replacements.forEach((r, j) => filled(r.site) && o.push({key:'r'+j, site:r.site.trim(), price:num(r.price), url:r.url,
    repl:true, mpn:r.mpn, note:r.description}));
  return o;
}
// auto = cheapest priced exact match, else the first exact match, else the first replacement
function pick(opts, c){
  const hit = opts.find(o => o.key === c.src);
  if (hit) return hit;
  const exact = opts.filter(o => !o.repl), priced = exact.filter(o => o.price != null);
  if (priced.length) return priced.reduce((a, b) => b.price < a.price ? b : a);
  return exact[0] || opts[0] || null;
}
function calc(p){
  const c = st.parts[p.mpn] || {};
  const inc = c.include ?? p.designators.length > 0;
  const auto = p.designators.length * st.boards;
  const need = num(c.need) ?? auto;
  const buf = num(c.buf) || 0;
  const wanted = c.unit === '%' ? need * (1 + buf / 100) : need + buf;
  const qty = Math.max(Math.ceil(wanted - 1e-9), Math.ceil(num(c.min) || 0), 0);
  const opts = options(p), opt = pick(opts, c);
  const up = opt ? opt.price : null;
  return {c, inc, auto, qty, opts, opt, up, total: up == null ? null : up * qty};
}
const needsFix = r => r.inc && r.qty > 0 && (!r.opt || (r.up == null && !isStock(r.opt.site)));

function buildOrders(){
  const sites = new Map(), noSource = [];
  for (const p of parts){
    const r = calc(p);
    if (!r.inc || r.qty <= 0) continue;
    if (!r.opt){ noSource.push(p.mpn); continue; }
    const key = r.opt.site.toLowerCase().replace(/\s+/g, ' ');
    let g = sites.get(key);
    if (!g) sites.set(key, g = {key, name:r.opt.site, lines:[], total:0, pcs:0, unpriced:0});
    if (g.name === g.name.toUpperCase() && r.opt.site !== r.opt.site.toUpperCase()) g.name = r.opt.site;
    g.lines.push({p, r}); g.pcs += r.qty;
    if (r.total != null) g.total += r.total; else if (!isStock(r.opt.site)) g.unpriced++;
  }
  return {sites, noSource};
}

// ---- rendering ------------------------------------------------------------
function visible(p){
  const q = $('#q').value.toLowerCase(), f = $('#filter').value, r = calc(p);
  const hay = [p.mpn, p.manufacturer, p.description, p.package, ...p.designators,
    ...p.sites.map(s => s.site), ...p.replacements.map(x => x.mpn + ' ' + x.site)].join(' ').toLowerCase();
  if (q && !hay.includes(q)) return false;
  return f === 'all' || (f === 'sel' && r.inc) || (f === 'unsel' && !r.inc) || (f === 'noprice' && needsFix(r));
}

function renderTable(){
  const html = parts.map((p, i) => {
    if (!visible(p)) return '';
    const r = calc(p), c = r.c;
    return `<tr data-i="${i}">
      <td><input type="checkbox" data-k="include" aria-label="Include ${esc(p.mpn)}"></td>
      <td><div class="m">${esc(p.mpn)}</div><div class="d">${esc(p.description)}</div>
        <div class="d">${p.designators.length ? esc(p.designators.join(', ')) : 'no designators'}</div></td>
      <td><input class="n" data-k="need" inputmode="numeric" value="${esc(c.need ?? '')}"></td>
      <td><div class="buf"><input data-k="buf" inputmode="decimal" value="${esc(c.buf ?? '')}" placeholder="0">
        <select data-k="unit"><option value="pcs">pcs</option><option value="%">%</option></select></div></td>
      <td><input class="n" data-k="min" inputmode="numeric" value="${esc(c.min ?? '')}" placeholder="0"></td>
      <td class="r qty"></td>
      <td><div class="src"><select data-k="src"><option value="">Auto</option>${r.opts.map(o =>
        `<option value="${o.key}">${o.repl ? '↻ ' : ''}${esc(o.site)}${o.repl ? ' (replacement)' : ''} · ${o.price == null ? 'no price' : unitPrice(o.price) + ' €'}</option>`).join('')}
        </select><a class="open" target="_blank" rel="noopener" title="Open product page">↗</a></div></td>
      <td class="r up"></td><td class="r lt"></td></tr>`;
  }).join('');
  $('#rows').innerHTML = html || '<tr><td colspan="9"><div class="empty">No parts match</div></td></tr>';
  $('#rows').querySelectorAll('tr[data-i]').forEach(tr => {
    const c = st.parts[parts[tr.dataset.i].mpn] || {};
    tr.querySelector('[data-k=unit]').value = c.unit || 'pcs';
    tr.querySelector('[data-k=src]').value = c.src || '';
    updateRow(tr);
  });
}

// refresh the computed cells of one row without touching the inputs (keeps focus while typing)
function updateRow(tr){
  const p = parts[tr.dataset.i], r = calc(p);
  tr.classList.toggle('off', !r.inc);
  tr.querySelector('[data-k=include]').checked = r.inc;
  tr.querySelector('[data-k=need]').placeholder = r.auto;
  tr.querySelector('.qty').textContent = r.qty;
  const src = tr.querySelector('[data-k=src]');
  src.options[0].textContent = r.opt && !r.c.src ? 'Auto → ' + r.opt.site : 'Auto';
  if (r.c.src && !r.opts.some(o => o.key === r.c.src)) src.value = '';
  const a = tr.querySelector('.open');
  a.href = r.opt ? url(r.opt.url) : ''; a.style.visibility = a.href ? '' : 'hidden';
  const bad = needsFix(r);
  tr.querySelector('.up').textContent = r.opt && isStock(r.opt.site) ? 'on site' : unitPrice(r.up);
  const lt = tr.querySelector('.lt');
  lt.textContent = r.total == null ? '' : money(r.total);
  lt.classList.toggle('warn', bad);
  tr.querySelector('.up').classList.toggle('warn', bad);
}

function renderSummary(){
  const {sites, noSource} = buildOrders();
  const groups = [...sites.values()].sort((a, b) => isStock(a.name) - isStock(b.name) || b.total - a.total);
  const total = groups.reduce((s, g) => s + g.total, 0);
  const lines = groups.reduce((s, g) => s + g.lines.length, 0), pcs = groups.reduce((s, g) => s + g.pcs, 0);
  const unpriced = groups.reduce((s, g) => s + g.unpriced, 0);
  $('#summary').innerHTML = `
    <div class="total"><div class="big">${money(total)} €</div>
      <div class="sub">${lines} parts · ${pcs} pieces · ${groups.length} site${groups.length === 1 ? '' : 's'} · ${st.boards} board${st.boards == 1 ? '' : 's'}</div>
      ${unpriced ? `<div class="sub warn">${unpriced} line${unpriced == 1 ? '' : 's'} without a price are not in the total</div>` : ''}
      ${noSource.length ? `<div class="sub warn">No site for: ${esc(noSource.join(', '))}</div>` : ''}</div>
    ${groups.map(g => `<div class="card"><div class="h"><b>${esc(g.name)}</b><span class="amt">${isStock(g.name) ? '' : money(g.total) + ' €'}</span></div>
      <div class="sub">${g.lines.length} part${g.lines.length == 1 ? '' : 's'} · ${g.pcs} pieces${g.unpriced ? ` · <span class="warn">${g.unpriced} unpriced</span>` : ''}</div>
      <button data-exp="${esc(g.key)}">Export CSV</button></div>`).join('') || '<div class="empty">Select parts to see the summary</div>'}`;
}

// ---- CSV export -----------------------------------------------------------
const csvCell = v => { const s = String(v ?? ''); return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
// the number a site's cart/BOM import wants, taken from the product URL (falls back to the MPN)
function supplierPn(u, mpn){
  return (/lcsc\.com.*\/(C\d+)\.html/i.exec(u || '') || /farnell\.com.*\/dp\/(\d+)/i.exec(u || '') || [])[1] || mpn;
}
function exportSite(key){
  const g = buildOrders().sites.get(key); if (!g) return;
  const rows = [['Supplier PN', 'MPN', 'Quantity', 'Manufacturer', 'Description', 'Unit price EUR', 'Line total EUR', 'Designators', 'URL', 'Note']];
  for (const {p, r} of g.lines){
    const o = r.opt, mpn = o.repl ? (o.mpn || '') : p.mpn;
    rows.push([supplierPn(o.url, mpn), mpn, r.qty, o.repl ? '' : p.manufacturer, p.description,
      r.up ?? '', r.total == null ? '' : +r.total.toFixed(4), p.designators.join(' '), o.url,
      o.repl ? `Replacement for ${p.mpn}${o.note ? ': ' + o.note : ''}` : '']);
  }
  const csv = rows.map(r => r.map(csvCell).join(',')).join('\r\n') + '\r\n';
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type:'text/csv'}));
  a.download = g.name.replace(/[^\w.-]+/g, '_').toLowerCase() + '_order.csv';
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---- state + saving -------------------------------------------------------
function setStatus(text, cls){ const el = $('#status'); el.textContent = text; el.className = cls || ''; }
function changed(){
  setStatus('unsaved changes', 'dirty');
  clearTimeout(saveTimer); saveTimer = setTimeout(save, 500);
  renderSummary();
}
async function save(){
  clearTimeout(saveTimer); saveTimer = null;
  try {
    const r = await fetch('/api/state', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(st), keepalive:true});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    setStatus('saved', 'saved');
  } catch (e) { setStatus('save failed: ' + e.message, 'dirty'); }
}
function setChoice(mpn, k, v){
  const c = st.parts[mpn] ||= {};
  if (v === '' || v == null) delete c[k]; else c[k] = v;
}

$('#rows').addEventListener('input', onEdit);
$('#rows').addEventListener('change', onEdit);
function onEdit(e){
  const k = e.target.dataset.k, tr = e.target.closest('tr[data-i]'); if (!k || !tr) return;
  if (e.type === 'input' && e.target.tagName === 'SELECT') return;   // selects are handled on 'change'
  setChoice(parts[tr.dataset.i].mpn, k, e.target.type === 'checkbox' ? e.target.checked : e.target.value.trim());
  updateRow(tr); changed();
}
$('#summary').addEventListener('click', e => { if (e.target.dataset.exp) exportSite(e.target.dataset.exp); });
$('#boards').addEventListener('input', e => {
  st.boards = Math.max(1, Math.floor(Number(e.target.value)) || 1);
  $('#rows').querySelectorAll('tr[data-i]').forEach(updateRow); changed();
});
for (const id of ['q', 'filter']) $('#' + id).addEventListener('input', renderTable);
for (const [id, on] of [['all', true], ['none', false]]) $('#' + id).addEventListener('click', () => {
  $('#rows').querySelectorAll('tr[data-i]').forEach(tr => { setChoice(parts[tr.dataset.i].mpn, 'include', on); updateRow(tr); });
  changed();
});
$('#reset').addEventListener('click', () => {
  if (!confirm('Forget all selections, quantities, buffers and source choices?')) return;
  st.parts = {}; renderTable(); changed();
});
addEventListener('beforeunload', () => { if (saveTimer) save(); });
// keep-alive: lets the server stop itself once every page is closed
new EventSource('/api/events');

(async () => {
  const [p, s] = await Promise.all([fetch('/api/parts').then(r => r.json()), fetch('/api/state').then(r => r.json())]);
  parts = Object.entries(p).map(([mpn, x]) => ({mpn, manufacturer:x.manufacturer || '', description:x.description || '',
    package:x.package || '', designators:x.designators || [], sites:x.sites || [], replacements:x.replacements || []}));
  st = {boards:s.boards || 1, parts:s.parts || {}};
  $('#boards').value = st.boards;
  renderTable(); renderSummary();
})();
</script></body></html>
"""


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yaml_file", nargs="?", type=Path, default=here / "parts_crossmatch.yaml")
    ap.add_argument("--state", type=Path, help="where your choices are saved (default: parts_order.json next to the yaml)")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--keep-alive", action="store_true",
                    help="keep the server running after the last browser tab is closed")
    args = ap.parse_args()

    path = args.yaml_file.resolve()
    if not path.is_file():
        ap.error(f"{path} not found")
    state_path = (args.state or path.with_name("parts_order.json")).resolve()
    clients = Clients()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(path, state_path, clients))
    except OSError as e:
        raise SystemExit(f"Cannot listen on port {args.port}: {e} (try --port)")
    url = f"http://localhost:{args.port}/"
    print(f"Server running. Open: {url}\nReading {path}\nSaving choices to {state_path}\n"
          + ("Ctrl+C to stop." if args.keep_alive else "Stops when you close the page (or Ctrl+C)."), flush=True)
    if not args.no_open:
        webbrowser.open(url)
    if not args.keep_alive:
        def watchdog():
            while not clients.all_closed():
                time.sleep(0.5)
            server.shutdown()
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
        print("Page closed, server stopped.")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
