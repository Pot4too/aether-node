#!/usr/bin/env python3
"""Interactive BOM review page for a KiCad project.

Usage:
    python3 bom_review.py [project_folder] [--port 8765] [--no-open]

project_folder is a KiCad project directory (relative or absolute, default: the
current directory). It must contain <folder_name>.csv, the exported BOM; for
example my-board/my-board.csv.

Starts a local web server (127.0.0.1 only) and opens the BOM as a web page where
each line can be ticked off and given a note. Changes are saved immediately to
<folder_name>.review.json inside the project folder, so you can close the tab or
stop the server and pick up where you left off. Each project keeps its own review
file, so the script can be used for as many boards as you like. The CSV is re-read
on every page load, so re-exporting the BOM from KiCad keeps your review state
(matched by Reference).

Expected CSV columns: Reference, Qty, Value, Footprint, Datasheet
"""
import argparse
import csv
import json
import os
import re
import select
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CSV_COLUMNS = ["Reference", "Qty", "Value", "Footprint", "Datasheet"]
MAX_BODY = 1_000_000
CLOSE_GRACE = 3  # wait after the last page disconnects, so a page reload doesn't stop the server

state_lock = threading.Lock()


class Clients:
    """Counts open browser pages so the server can stop once the last one is closed.

    Each page holds one long-lived /api/events connection; when the tab closes the
    browser drops it, which is detected immediately (no reliance on unload events).
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.open = 0
        self.ever_connected = False
        self.empty_since = 0.0

    def connect(self) -> None:
        with self.lock:
            self.open += 1
            self.ever_connected = True

    def disconnect(self) -> None:
        with self.lock:
            self.open -= 1
            if not self.open:
                self.empty_since = time.monotonic()

    def all_closed(self) -> bool:
        with self.lock:
            return self.ever_connected and not self.open and time.monotonic() - self.empty_since > CLOSE_GRACE


def ref_key(reference: str) -> str:
    return re.sub(r"\s+", "", reference)


def clean_datasheet(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    # LCSC links carry long tracking query strings; the bare PDF URL works.
    if parts.scheme in ("http", "https") and parts.netloc.endswith("lcsc.com"):
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return value


def read_bom(csv_path: Path) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{csv_path}: missing column(s): {', '.join(missing)}")
        rows = [r for r in reader if any((r[c] or "").strip() for c in CSV_COLUMNS)]
    return [
        {
            "key": ref_key(r["Reference"]),
            "reference": r["Reference"].replace(",", ", ").strip(),
            "qty": r["Qty"].strip(),
            "value": r["Value"].strip(),
            "footprint": r["Footprint"].strip(),
            "datasheet": clean_datasheet(r["Datasheet"]),
        }
        for r in rows
    ]


def read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict) -> None:
    # Write-then-rename so a crash mid-write cannot corrupt the saved review.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def render_page(csv_path: Path, state_path: Path) -> bytes:
    data = {"title": csv_path.stem, "rows": read_bom(csv_path), "state": read_state(state_path)}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__DATA__", payload).replace("__TITLE__", csv_path.stem).encode("utf-8")


def make_handler(csv_path: Path, state_path: Path, clients: Clients):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _hold_open(self) -> None:
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

        def do_GET(self) -> None:
            if self.path == "/api/events":
                return self._hold_open()
            if self.path.split("?")[0] != "/":
                return self._send(404, b"not found", "text/plain")
            try:
                self._send(200, render_page(csv_path, state_path), "text/html; charset=utf-8")
            except (OSError, ValueError, SystemExit) as e:
                self._send(500, f"Could not build page: {e}".encode(), "text/plain; charset=utf-8")

        def do_PUT(self) -> None:
            # Only accept writes from our own page (guards against other sites
            # posting to localhost, and DNS rebinding).
            origin = self.headers.get("Origin")
            if self.path != "/api/row" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                return self._send(403, b"forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY:
                    return self._send(413, b"too large", "text/plain")
                body = json.loads(self.rfile.read(length))
                key, checked, notes = body["key"], bool(body["checked"]), str(body["notes"])
                if not isinstance(key, str) or not key:
                    raise ValueError("bad key")
            except (ValueError, KeyError, TypeError):
                return self._send(400, b"bad request", "text/plain")
            with state_lock:
                try:
                    state = read_state(state_path)
                    if checked or notes:
                        state[key] = {"checked": checked, "notes": notes}
                    else:
                        state.pop(key, None)
                    write_state(state_path, state)
                except (OSError, ValueError) as e:
                    return self._send(500, f"save failed: {e}".encode(), "text/plain; charset=utf-8")
            self._send(200, b"ok", "text/plain")

        def log_message(self, fmt, *args) -> None:
            pass

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOM Review: __TITLE__</title>
<style>
:root {
  --bg: #f6f7f9; --surface: #fff; --text: #1c2026; --muted: #667085; --line: #e1e4ea;
  --accent: #2563eb; --done-bg: #ecfdf3; --done-line: #b7ebcb; --ok: #12805c; --err: #b42318;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --surface: #1c2027; --text: #e6e8ec; --muted: #8b93a1; --line: #2d333d;
    --accent: #6ea0ff; --done-bg: #14261d; --done-line: #25503a; --ok: #4ad19b; --err: #ff8a80;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header { position: sticky; top: 0; z-index: 2; background: var(--surface);
  border-bottom: 1px solid var(--line); padding: 12px 20px; }
.top { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; }
h1 { font-size: 17px; margin: 0; font-weight: 650; }
.progress { flex: 1 1 220px; display: flex; align-items: center; gap: 10px; min-width: 200px; }
.bar { flex: 1; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; }
.bar > div { height: 100%; width: 0; background: var(--ok); transition: width .2s; }
#count { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
#save { min-width: 70px; text-align: right; color: var(--muted); font-size: 12px; }
#save.err { color: var(--err); font-weight: 600; }
.tools { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
input[type=search], select, textarea { font: inherit; color: var(--text); background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px; }
input[type=search] { width: 240px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
main { padding: 16px 20px 40px; overflow-x: auto; }
table { border-collapse: separate; border-spacing: 0; width: 100%; min-width: 900px;
  background: var(--surface); border: 1px solid var(--line); border-radius: 8px; }
th { text-align: left; font-size: 12px; color: var(--muted); font-weight: 600;
  padding: 9px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
tr.done td { background: var(--done-bg); }
td.chk { width: 44px; text-align: center; }
td.chk input { width: 18px; height: 18px; cursor: pointer; accent-color: var(--ok); margin-top: 2px; }
td.ref { font-weight: 600; min-width: 110px; }
td.qty { text-align: right; font-variant-numeric: tabular-nums; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); word-break: break-all; }
td.notes { min-width: 240px; width: 28%; }
textarea { width: 100%; min-height: 32px; resize: none; overflow: hidden; display: block; }
a { color: var(--accent); }
.empty { padding: 30px; text-align: center; color: var(--muted); }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>BOM Review: __TITLE__</h1>
    <div class="progress"><div class="bar"><div id="fill"></div></div><span id="count"></span></div>
    <span id="save" aria-live="polite"></span>
  </div>
  <div class="tools">
    <input type="search" id="q" placeholder="Search reference, value, footprint, notes" aria-label="Search">
    <select id="filter" aria-label="Filter">
      <option value="all">All lines</option>
      <option value="todo">Not checked</option>
      <option value="done">Checked</option>
      <option value="notes">With notes</option>
    </select>
  </div>
</header>
<main>
  <table>
    <thead><tr>
      <th aria-label="Checked"></th><th>Reference</th><th>Qty</th><th>Value</th>
      <th>Footprint</th><th>Datasheet</th><th>Notes</th>
    </tr></thead>
    <tbody id="body"></tbody>
  </table>
  <div class="empty" id="empty" hidden>No lines match.</div>
</main>
<script>
const DATA = __DATA__;
const state = DATA.state;           // key -> {checked, notes}
const $ = id => document.getElementById(id);
const get = k => state[k] || { checked: false, notes: "" };

// ---- saving -------------------------------------------------------------
let pending = 0, failed = false;
function setSave(text, err) { const s = $("save"); s.textContent = text; s.className = err ? "err" : ""; }
const timers = {};
function save(key, immediate) {
  clearTimeout(timers[key]);
  const send = () => {
    delete timers[key];
    const v = get(key);
    pending++; setSave("Saving…");
    fetch("/api/row", { method: "PUT", keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, checked: v.checked, notes: v.notes }) })
      .then(r => { if (!r.ok) throw new Error(r.status); failed = false; })
      .catch(() => { failed = true; })
      .finally(() => { pending--; if (!pending) failed ? setSave("Not saved! Is the server running?", true) : setSave("Saved"); });
  };
  immediate ? send() : (timers[key] = setTimeout(send, 400));
}
// ---- keep-alive: lets the server stop itself once every page is closed --
// The browser drops this connection when the tab closes; the server notices.
new EventSource("/api/events");
window.addEventListener("pagehide", () => {
  for (const k of Object.keys(timers)) save(k, true);   // flush unsaved notes first
});

// ---- rendering ----------------------------------------------------------
function datasheetCell(td, v) {
  if (/^https?:\/\//i.test(v)) {
    const a = document.createElement("a");
    a.href = v; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.textContent = decodeURIComponent(new URL(v).pathname.split("/").pop() || new URL(v).host);
    td.appendChild(a);
  } else td.textContent = v;
}
function autosize(t) { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; }

const rowEls = DATA.rows.map(r => {
  const tr = document.createElement("tr");
  const cell = (cls, text) => { const td = document.createElement("td"); if (cls) td.className = cls; if (text) td.textContent = text; tr.appendChild(td); return td; };
  const chk = document.createElement("input");
  chk.type = "checkbox"; chk.setAttribute("aria-label", "Checked: " + r.reference);
  cell("chk").appendChild(chk);
  cell("ref", r.reference); cell("qty", r.qty); cell("", r.value);
  if (r.footprint) { const c = document.createElement("code"); c.textContent = r.footprint; cell("").appendChild(c); } else cell("");
  datasheetCell(cell(""), r.datasheet);
  const ta = document.createElement("textarea");
  ta.rows = 1; ta.setAttribute("aria-label", "Notes: " + r.reference);
  cell("notes").appendChild(ta);

  chk.checked = get(r.key).checked; ta.value = get(r.key).notes;
  tr.classList.toggle("done", chk.checked);
  chk.addEventListener("change", () => {
    state[r.key] = { ...get(r.key), checked: chk.checked };
    tr.classList.toggle("done", chk.checked);
    save(r.key, true); update();
  });
  ta.addEventListener("input", () => {
    state[r.key] = { ...get(r.key), notes: ta.value };
    autosize(ta); save(r.key); update(true);
  });
  const hay = () => [r.reference, r.value, r.footprint, r.datasheet, get(r.key).notes].join(" ").toLowerCase();
  return { tr, r, ta, hay };
});
$("body").append(...rowEls.map(e => e.tr));

function update(keepFilter) {
  const done = DATA.rows.filter(r => get(r.key).checked).length;
  $("count").textContent = done + " / " + DATA.rows.length + " checked";
  $("fill").style.width = (DATA.rows.length ? 100 * done / DATA.rows.length : 0) + "%";
  if (keepFilter === true && $("filter").value !== "notes") return;   // don't hide a row while typing in it
  const q = $("q").value.trim().toLowerCase(), f = $("filter").value;
  let shown = 0;
  for (const e of rowEls) {
    const v = get(e.r.key);
    const ok = (f === "all" || (f === "todo" && !v.checked) || (f === "done" && v.checked) || (f === "notes" && v.notes.trim()))
      && (!q || e.hay().includes(q));
    e.tr.hidden = !ok; if (ok) shown++;
  }
  $("empty").hidden = shown > 0;
}
$("q").addEventListener("input", () => update());
$("filter").addEventListener("change", () => update());
update();
rowEls.forEach(e => autosize(e.ta));
window.addEventListener("resize", () => rowEls.forEach(e => autosize(e.ta)));
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", type=Path, default=Path("."),
                    help="KiCad project folder containing <folder_name>.csv (default: current directory)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--keep-alive", action="store_true",
                    help="keep the server running after the last browser tab is closed")
    args = ap.parse_args()

    project = args.project.expanduser().resolve()
    if not project.is_dir():
        raise SystemExit(f"{project} is not a directory")
    csv_path = project / f"{project.name}.csv"
    if not csv_path.exists():
        found = sorted(p.name for p in project.glob("*.csv"))
        hint = f" (found: {', '.join(found)})" if found else ""
        raise SystemExit(f"{csv_path} not found{hint}")
    state_path = project / f"{project.name}.review.json"
    read_bom(csv_path)  # fail early on a bad CSV

    clients = Clients()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(csv_path, state_path, clients))
    except OSError as e:
        raise SystemExit(f"Cannot listen on port {args.port}: {e} (try --port)")
    url = f"http://localhost:{args.port}/"
    print(f"Server running. Open: {url}\nSaving to {state_path}\n"
          + ("Ctrl+C to stop." if args.keep_alive else "Stops when you close the page (or Ctrl+C)."), flush=True)
    if not args.no_open:
        webbrowser.open(url)
    if not args.keep_alive:
        def watchdog() -> None:
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
