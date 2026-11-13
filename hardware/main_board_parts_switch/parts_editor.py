#!/usr/bin/env python3
"""Web GUI for viewing and editing parts_crossmatch.yaml.

Usage:
    python3 parts_editor.py [yaml_file] [--port 8766] [--no-open] [--keep-alive]

yaml_file defaults to parts_crossmatch.yaml next to this script. Starts a local
web server (127.0.0.1 only) with a searchable list of parts and a form for each
one (info, designators, exact-match sites, replacements). Nothing is written
until you press Save (or Ctrl+S). The server stops by itself when you close the
page (or Ctrl+C); pass --keep-alive to keep it running. The leading '#' comment block of the file is
kept; the rest is rewritten in a stable format, so git diffs stay small.

Requires PyYAML.
"""
import argparse
import json
import os
import select
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:
    raise SystemExit(f"PyYAML is required but not installed for {sys.executable}.\n"
                     f"Install it with:  {sys.executable} -m pip install pyyaml\n"
                     "(if pip refuses on Homebrew Python, add --break-system-packages or use a venv)")

MAX_BODY = 10_000_000
CLOSE_GRACE = 3  # wait after the last page disconnects, so a page reload doesn't stop the server
PART_FIELDS = ("manufacturer", "description", "package")
SITE_FIELDS = ("site", "url", "price")
REPL_FIELDS = ("mpn", "description", "site", "url", "price")


class Clients:
    """Counts open browser pages so the server can stop once the last one is closed.

    Each page holds one long-lived /api/events connection; when the tab closes the
    browser drops it, which is detected immediately (no reliance on unload events).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.open = 0
        self.ever_connected = False
        self.empty_since = 0.0

    def connect(self):
        with self.lock:
            self.open += 1
            self.ever_connected = True

    def disconnect(self):
        with self.lock:
            self.open -= 1
            if not self.open:
                self.empty_since = time.monotonic()

    def all_closed(self):
        with self.lock:
            return self.ever_connected and not self.open and time.monotonic() - self.empty_since > CLOSE_GRACE


class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


SCHEMA_KEYS = {"parts", "designators", "sites", "replacements", "site", "url", "price", "mpn", *PART_FIELDS}


def _str(dumper, value):
    style = None if value in SCHEMA_KEYS else '"'
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


Dumper.add_representer(str, _str)


def read_header(path):
    """Leading comment/blank lines before the first YAML key."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "".join(l + "\n" for l in lines)


def load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("parts") or {}


def clean_price(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid price: {value!r}")


def normalize(parts):
    """Validate the payload from the browser and coerce it to the file schema."""
    if not isinstance(parts, dict):
        raise ValueError("parts must be an object")
    out = {}
    for mpn, p in parts.items():
        mpn = str(mpn).strip()
        if not mpn:
            raise ValueError("a part has an empty MPN")
        if mpn in out:
            raise ValueError(f"duplicate MPN: {mpn}")
        part = {k: str(p.get(k) or "") for k in PART_FIELDS}
        part["designators"] = [str(d).strip() for d in p.get("designators") or [] if str(d).strip()]
        part["sites"] = [
            {**{k: str(s.get(k) or "") for k in ("site", "url")}, "price": clean_price(s.get("price"))}
            for s in p.get("sites") or []
        ]
        part["replacements"] = [
            {
                **{k: str(r.get(k) or "") for k in ("mpn", "description", "site", "url")},
                "price": clean_price(r.get("price")),
            }
            for r in p.get("replacements") or []
        ]
        out[mpn] = part
    return out


def save(path, header, parts):
    body = yaml.dump({"parts": parts}, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=1000)
    # blank line between parts, like the hand-written file
    body = body.replace("\n  \"", "\n\n  \"")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(header + body)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def make_handler(path, header, clients):
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
            else:
                self._send(404, '{"error":"not found"}')

        def do_PUT(self):
            # Only accept writes from our own page (guards against other sites
            # posting to localhost, and DNS rebinding).
            origin = self.headers.get("Origin")
            if self.path != "/api/parts" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                return self._send(403, '{"error":"forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, '{"error":"too large"}')
            try:
                parts = normalize(json.loads(self.rfile.read(length)))
                with lock:
                    save(path, header, parts)
                self._send(200, json.dumps({"ok": True, "count": len(parts)}))
            except OSError as e:
                self._send(500, json.dumps({"error": f"save failed: {e}"}))
            except (ValueError, AttributeError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}))

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parts editor</title>
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
#status{color:var(--mute)}
#status.dirty{color:var(--bad)}
#status.saved{color:var(--ok)}
main{flex:1;display:flex;min-height:0}
aside{width:320px;border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
aside .tools{padding:8px;display:flex;flex-direction:column;gap:6px;border-bottom:1px solid var(--line)}
#list{overflow:auto;flex:1}
.item{padding:7px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.item:hover{background:var(--bg)}
.item.sel{background:color-mix(in srgb,var(--acc) 15%,transparent)}
.item .m{font-weight:600;word-break:break-all}
.item .d{color:var(--mute);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{font-size:11px;color:var(--mute)}
.badge.warn{color:var(--bad)}
section{flex:1;overflow:auto;padding:16px 20px}
input,textarea,button{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:5px 8px}
input,textarea{width:100%}
button{cursor:pointer;background:var(--panel)}
button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button.danger:hover{border-color:var(--bad);color:var(--bad)}
.grid{display:grid;grid-template-columns:140px 1fr;gap:8px 12px;align-items:center;max-width:900px}
.grid label{color:var(--mute)}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:24px 0 8px;display:flex;gap:10px;align-items:center}
table{border-collapse:collapse;width:100%}
th{text-align:left;font-weight:500;color:var(--mute);font-size:12px;padding:0 4px 3px}
td{padding:2px 4px;vertical-align:top}
td.x{width:1%}
.open{text-decoration:none;color:var(--acc);padding:5px 4px}
.empty{color:var(--mute);margin:60px auto;text-align:center}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:16px}
.bar .t{font-size:18px;font-weight:600;flex:1;word-break:break-all}
</style></head><body>
<header>
  <h1>Parts editor</h1><span class="file">__FILE__</span><span class="sp"></span>
  <span id="status"></span>
  <button class="primary" id="save">Save</button>
</header>
<main>
  <aside>
    <div class="tools">
      <input id="q" placeholder="Search MPN, description, designator, site…">
      <select id="filter" aria-label="Filter">
        <option value="all">All parts</option>
        <option value="noprice">Missing a price</option>
        <option value="noexact">No exact match</option>
        <option value="norepl">No replacement</option>
        <option value="none">No exact match and no replacement</option>
      </select>
      <button id="add">+ New part</button>
    </div>
    <div id="list"></div>
  </aside>
  <section id="detail"></section>
</main>
<script>
let parts = [];          // array of {mpn, manufacturer, description, package, designators[], sites[], replacements[]}
let sel = null, dirty = false;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function setDirty(d, msg){
  dirty = d;
  const el = $('#status');
  el.textContent = msg ?? (d ? 'unsaved changes' : '');
  el.className = d ? 'dirty' : (msg ? 'saved' : '');
}
const filled = v => String(v ?? '').trim() !== '';
const hasExact = p => p.sites.some(s => filled(s.url));
const hasRepl = p => p.replacements.some(r => filled(r.mpn) || filled(r.url));
const FILTERS = {
  all: () => true,
  noprice: p => !hasPrice(p),
  noexact: p => !hasExact(p),
  norepl: p => !hasRepl(p),
  none: p => !hasExact(p) && !hasRepl(p),
};
const hasPrice = p => p.sites.some(s => s.price != null && s.price !== '');
const blank = () => ({mpn:'', manufacturer:'', description:'', package:'', designators:[],
  sites:[{site:'',url:'',price:null}], replacements:[]});

async function load(){
  const r = await (await fetch('/api/parts')).json();
  parts = Object.entries(r).map(([mpn, p]) => ({mpn, manufacturer:'', description:'', package:'',
    designators:[], sites:[], replacements:[], ...p}));
  sel = parts.length ? 0 : null;
  render(); setDirty(false);
}

function renderList(){
  const q = $('#q').value.toLowerCase(), show = FILTERS[$('#filter').value];
  $('#list').innerHTML = parts.map((p, i) => {
    const hay = [p.mpn, p.manufacturer, p.description, p.package, ...p.designators,
      ...p.sites.map(s => s.site), ...p.replacements.map(r => r.mpn + ' ' + r.site)].join(' ').toLowerCase();
    if (q && !hay.includes(q)) return '';
    if (!show(p)) return '';
    const n = p.sites.filter(s => filled(s.url)).length;
    return `<div class="item ${i===sel?'sel':''}" data-i="${i}">
      <div class="m">${esc(p.mpn) || '<i>(new)</i>'}</div>
      <div class="d">${esc(p.description)}</div>
      <div class="badge ${hasPrice(p)?'':'warn'}">${n} site${n==1?'':'s'} · ${hasPrice(p)?'priced':'no price'} · ${p.replacements.filter(r => filled(r.mpn) || filled(r.url)).length} repl.</div>
    </div>`;
  }).join('') || '<div class="empty">No parts match</div>';
}

const cell = (path, v, ph='', type='text') =>
  `<input data-f="${path}" value="${esc(v)}" placeholder="${ph}" ${type==='num'?'inputmode="decimal"':''}>`;

function renderDetail(){
  const el = $('#detail');
  const p = parts[sel];
  if (!p){ el.innerHTML = '<div class="empty">Select a part or create a new one</div>'; return; }
  const link = u => u ? `<a class="open" href="${esc(u)}" target="_blank" rel="noopener" title="Open">↗</a>` : '';
  el.innerHTML = `
    <div class="bar"><div class="t">${esc(p.mpn) || '(new part)'}</div>
      <button class="danger" id="del">Delete part</button></div>
    <div class="grid">
      <label>MPN (key)</label>${cell('mpn', p.mpn)}
      <label>Manufacturer</label>${cell('manufacturer', p.manufacturer)}
      <label>Description</label>${cell('description', p.description)}
      <label>Package</label>${cell('package', p.package)}
      <label>Designators</label><input data-f="designators" value="${esc(p.designators.join(', '))}" placeholder="C1, C2, R5">
    </div>
    <h2>Exact-match sites <button data-add="sites">+ add</button></h2>
    <table><tr><th style="width:130px">Site</th><th>URL</th><th style="width:90px">Price €</th><th></th><th></th></tr>
    ${p.sites.map((s,j) => `<tr>
      <td>${cell(`sites.${j}.site`, s.site, 'LCSC')}</td>
      <td>${cell(`sites.${j}.url`, s.url, 'https://…')}</td>
      <td>${cell(`sites.${j}.price`, s.price ?? '', '', 'num')}</td>
      <td class="x">${link(s.url)}</td>
      <td class="x"><button class="danger" data-rm="sites.${j}">✕</button></td></tr>`).join('')}
    </table>
    <h2>Replacements (not identical) <button data-add="replacements">+ add</button></h2>
    <table><tr><th style="width:150px">MPN</th><th>What differs</th><th style="width:110px">Site</th><th>URL</th><th style="width:90px">Price €</th><th></th><th></th></tr>
    ${p.replacements.map((r,j) => `<tr>
      <td>${cell(`replacements.${j}.mpn`, r.mpn)}</td>
      <td><textarea rows="2" data-f="replacements.${j}.description">${esc(r.description)}</textarea></td>
      <td>${cell(`replacements.${j}.site`, r.site)}</td>
      <td>${cell(`replacements.${j}.url`, r.url, 'https://…')}</td>
      <td>${cell(`replacements.${j}.price`, r.price ?? '', '', 'num')}</td>
      <td class="x">${link(r.url)}</td>
      <td class="x"><button class="danger" data-rm="replacements.${j}">✕</button></td></tr>`).join('')}
    </table>`;
}

function render(){ renderList(); renderDetail(); }

function setField(path, value){
  const p = parts[sel], k = path.split('.');
  if (k[0] === 'designators') p.designators = value.split(',').map(s => s.trim()).filter(Boolean);
  else if (k.length === 1) p[k[0]] = value;
  else p[k[0]][+k[1]][k[2]] = k[2] === 'price' ? (value.trim() === '' ? null : value.trim()) : value;
}

$('#detail').addEventListener('input', e => {
  const f = e.target.dataset.f; if (!f) return;
  setField(f, e.target.value); setDirty(true);
  if (f === 'mpn') $('.bar .t').textContent = parts[sel].mpn || '(new part)';
  renderList();
});
$('#detail').addEventListener('click', e => {
  const t = e.target, p = parts[sel];
  if (t.dataset.add){
    p[t.dataset.add].push(t.dataset.add === 'sites' ? {site:'',url:'',price:null}
      : {mpn:'',description:'',site:'',url:'',price:null});
    setDirty(true); render();
  } else if (t.dataset.rm){
    const [k, j] = t.dataset.rm.split('.'); p[k].splice(+j, 1); setDirty(true); render();
  } else if (t.id === 'del'){
    if (!confirm(`Delete ${p.mpn || 'this part'}?`)) return;
    parts.splice(sel, 1); sel = parts.length ? Math.min(sel, parts.length - 1) : null;
    setDirty(true); render();
  }
});
$('#list').addEventListener('click', e => {
  const it = e.target.closest('.item'); if (!it) return;
  sel = +it.dataset.i; render();
});
$('#q').addEventListener('input', renderList);
$('#filter').addEventListener('change', renderList);
$('#add').addEventListener('click', () => {
  $('#q').value = ''; $('#filter').value = 'all';
  parts.unshift(blank()); sel = 0; setDirty(true); render();
  document.querySelector('[data-f=mpn]').focus();
  $('#list').scrollTop = 0;
});

async function save(){
  const out = {};
  for (const p of parts){
    const key = p.mpn.trim();
    if (!key) return alert('A part has an empty MPN.');
    if (key in out) return alert('Duplicate MPN: ' + key);
    out[key] = {manufacturer:p.manufacturer, description:p.description, package:p.package,
      designators:p.designators, sites:p.sites, replacements:p.replacements};
  }
  const r = await fetch('/api/parts', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(out)});
  const j = await r.json();
  if (!r.ok) return alert('Save failed: ' + j.error);
  parts.forEach(p => p.mpn = p.mpn.trim());
  setDirty(false, `saved ${j.count} parts`);
}
$('#save').addEventListener('click', save);
addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 's'){ e.preventDefault(); save(); } });
addEventListener('beforeunload', e => { if (dirty){ e.preventDefault(); e.returnValue = ''; } });
// keep-alive: lets the server stop itself once every page is closed
new EventSource('/api/events');
load();
</script></body></html>
"""


def main():
    default = Path(__file__).resolve().parent / "parts_crossmatch.yaml"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("yaml_file", nargs="?", type=Path, default=default)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--keep-alive", action="store_true",
                    help="keep the server running after the last browser tab is closed")
    args = ap.parse_args()

    path = args.yaml_file.resolve()
    if not path.is_file():
        ap.error(f"{path} not found")
    header = read_header(path)
    clients = Clients()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(path, header, clients))
    except OSError as e:
        raise SystemExit(f"Cannot listen on port {args.port}: {e} (try --port)")
    url = f"http://localhost:{args.port}/"
    print(f"Server running. Open: {url}\nEditing {path}\n"
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
