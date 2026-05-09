#!/usr/bin/env python3
"""triage_viewer.py - local-only web viewer and FTS search console.

Serves http://127.0.0.1:8765/

UI:
  - Searchable, sortable table of the keep pile
  - SQLite/FTS5 search across all indexed mail
  - Click a row -> header + body + attachment list rendered safely
  - HTML bodies rendered inside a sandboxed iframe (no scripts, no remote loads)
"""

from __future__ import annotations

import email
import email.policy
import html
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(os.environ.get("EMAIL_PURGE_DIR", "~/email-purge")).expanduser()
KEEP_JSONL = Path(os.environ.get("EMAIL_KEEP_JSONL", BASE_DIR / "triage" / "keep.jsonl"))
DEFER_JSONL = Path(os.environ.get("EMAIL_DEFER_JSONL", BASE_DIR / "triage" / "defer.jsonl"))
EML_ROOT = Path(os.environ.get("EMAIL_EML_ROOT", BASE_DIR / "eml")).expanduser().resolve()
DB_PATH = Path(os.environ.get("EMAIL_INDEX_DB", BASE_DIR / "email_index.db")).expanduser()
HOST = "127.0.0.1"
PORT = 8765

# --- index load ---

def load_records(jsonl_path: Path) -> list[dict]:
    out = []
    if not jsonl_path.exists():
        return out
    with jsonl_path.open(encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rec["_id"] = i
            out.append(rec)
    return out


KEEP = load_records(KEEP_JSONL)
DEFER = load_records(DEFER_JSONL)
BY_PATH = {r["path"]: r for r in KEEP}
DEFER_BY_PATH = {r["path"]: r for r in DEFER}
KEEP_PATHS = set(BY_PATH)

print(f"[viewer] loaded {len(KEEP)} keep records, {len(DEFER)} defer records")


# --- SQLite FTS5 search ---

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def make_fts_query(query: str, raw: bool = False) -> str:
    """Convert a human search string into a conservative FTS5 query.

    Raw mode is still available for deliberate FTS5 syntax. Normal mode treats
    punctuation as separators so searches like an email address do not 500.
    """
    query = query.strip()
    if raw:
        return query
    phrases = re.findall(r'"([^"]+)"', query)
    without_phrases = re.sub(r'"[^"]+"', " ", query)
    tokens = re.findall(r"[\w]+", without_phrases, flags=re.UNICODE)
    parts = [f'"{p.replace(chr(34), chr(34) + chr(34))}"' for p in phrases if p.strip()]
    parts.extend(f'"{t}"' for t in tokens if t.strip())
    return " AND ".join(parts)


def safe_path(path_str: str) -> Path:
    p = Path(path_str).resolve()
    try:
        p.relative_to(EML_ROOT)
    except ValueError as exc:
        raise ValueError("path outside root") from exc
    return p


def send_attachment(handler: BaseHTTPRequestHandler, p: Path, part: int, inline: bool):
    got = get_attachment_bytes(p, part)
    if got is None:
        handler._send_json({"error": "part not found"}, status=404)
        return
    fname, ctype, data = got
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    disp_kind = "inline" if inline else "attachment"
    safe = fname.replace('"', "_")
    handler.send_header(
        "Content-Disposition",
        f'{disp_kind}; filename="{safe}"; filename*=UTF-8\'\'{urllib.parse.quote(fname)}',
    )
    handler.end_headers()
    handler.wfile.write(data)


def search_emails(query: str, scope: str = "all", limit: int = 200, offset: int = 0, raw: bool = False) -> dict:
    """FTS5 search across the email index.

    scope: 'all' | 'keep' | 'defer' | 'folder:<name>'
    Returns {results: [...], total: int, query: str, scope: str}
    """
    conn = get_db()
    try:
        fts_query = make_fts_query(query, raw=raw)
        if not fts_query:
            return {"results": [], "total": 0, "query": query, "scope": scope}

        where_extra = ""
        params_extra = []
        if scope == "keep":
            paths = list(KEEP_PATHS)
            if not paths:
                return {"results": [], "total": 0, "query": query, "scope": scope}
            placeholders = ",".join("?" for _ in paths)
            where_extra = f"AND e.path IN ({placeholders})"
            params_extra = paths
        elif scope == "defer":
            paths = list(KEEP_PATHS)
            if not paths:
                return {"results": [], "total": 0, "query": query, "scope": scope}
            placeholders = ",".join("?" for _ in paths)
            where_extra = f"AND e.path NOT IN ({placeholders})"
            params_extra = paths
        elif scope.startswith("folder:"):
            folder = scope.split(":", 1)[1]
            where_extra = "AND e.folder = ?"
            params_extra = [folder]

        count_sql = f"""
            SELECT COUNT(*)
            FROM emails_fts f
            JOIN emails e ON e.id = f.rowid
            WHERE emails_fts MATCH ? {where_extra}
        """
        total = conn.execute(count_sql, [fts_query] + params_extra).fetchone()[0]

        search_sql = f"""
            SELECT
                e.id, e.path, e.folder, e.subject, e.from_addr, e.to_addrs,
                e.date, e.has_attachments, e.attachment_count, e.body_size,
                snippet(emails_fts, 3, '<mark>', '</mark>', '...', 40) as snippet
            FROM emails_fts f
            JOIN emails e ON e.id = f.rowid
            WHERE emails_fts MATCH ? {where_extra}
            ORDER BY
                CASE WHEN e.date IS NOT NULL THEN e.date ELSE '0000' END DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(search_sql, [fts_query] + params_extra + [limit, offset]).fetchall()

        results = []
        for row in rows:
            triage = None
            triage_status = None
            path_str = row["path"]
            if path_str in BY_PATH:
                triage = BY_PATH[path_str]
                triage_status = "keep"
            elif path_str in DEFER_BY_PATH:
                triage = DEFER_BY_PATH[path_str]
                triage_status = "defer"

            results.append({
                "id": row["id"],
                "path": path_str,
                "folder": row["folder"],
                "subject": row["subject"] or "",
                "from": row["from_addr"] or "",
                "to": row["to_addrs"] or "",
                "date": row["date"] or "",
                "has_attachments": row["has_attachments"],
                "attachment_count": row["attachment_count"],
                "body_size": row["body_size"],
                "snippet": row["snippet"] or "",
                "triage_status": triage_status,
                "triage_reasons": triage.get("reasons", []) if triage else [],
            })

        return {
            "results": results,
            "total": total,
            "query": query,
            "fts_query": fts_query,
            "scope": scope,
            "offset": offset,
            "limit": limit,
        }
    finally:
        conn.close()


# --- email rendering ---

def decode_str(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def render_eml(path: Path) -> dict:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    headers = {
        "From": decode_str(msg.get("From", "")),
        "To": decode_str(msg.get("To", "")),
        "Cc": decode_str(msg.get("Cc", "")),
        "Subject": decode_str(msg.get("Subject", "")),
        "Date": decode_str(msg.get("Date", "")),
    }

    text_body = None
    html_body = None
    attachments = []

    # We index every leaf part (in walk order) so we can serve any of them
    # back by index. Whether something is "an attachment" is just for display.
    part_idx = -1
    for part in msg.walk():
        if part.is_multipart():
            continue
        part_idx += 1
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get("Content-Disposition") or "").lower()
        fname = part.get_filename()
        if fname:
            fname = decode_str(fname)

        if "attachment" in disp or fname:
            try:
                payload = part.get_payload(decode=True) or b""
                size = len(payload)
            except Exception:
                size = 0
            attachments.append({
                "filename": fname or "(unnamed)",
                "content_type": ctype,
                "size": size,
                "part": part_idx,
            })
            continue

        if ctype == "text/plain" and text_body is None:
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text_body = payload.decode(charset, errors="replace")
            except Exception:
                pass
        elif ctype == "text/html" and html_body is None:
            try:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")
            except Exception:
                pass

    return {
        "headers": headers,
        "text_body": text_body,
        "html_body": html_body,
        "attachments": attachments,
    }


def get_attachment_bytes(path: Path, part_idx: int):
    """Return (filename, content_type, bytes) for the given leaf-part index, or None."""
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    i = -1
    for part in msg.walk():
        if part.is_multipart():
            continue
        i += 1
        if i != part_idx:
            continue
        ctype = (part.get_content_type() or "application/octet-stream").lower()
        fname = decode_str(part.get_filename() or f"part{part_idx}")
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        return (fname, ctype, payload)
    return None


# --- HTTP handler ---

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Email triage search</title>
<style>
  body { font: 13px/1.4 system-ui, sans-serif; margin: 0; background: #f7f7f8; color: #222; }
  header { padding: 8px 12px; background: #fff; border-bottom: 1px solid #ddd; position: sticky; top: 0; z-index: 5; }
  header h1 { margin: 0 0 4px 0; font-size: 15px; }
  header .meta { color: #666; font-size: 12px; }
  #search { width: min(520px, 38vw); padding: 4px 8px; font: inherit; border: 1px solid #ccc; border-radius: 4px; }
  select, button { padding: 4px 8px; font: inherit; border: 1px solid #ccc; border-radius: 4px; background: #fff; }
  button { cursor: pointer; }
  main { display: flex; height: calc(100vh - 72px); }
  #list { flex: 0 0 50%; overflow: auto; background: #fff; border-right: 1px solid #ddd; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th { position: sticky; top: 0; background: #f0f0f2; padding: 4px 6px; text-align: left; cursor: pointer; user-select: none; border-bottom: 1px solid #ccc; }
  tbody tr { cursor: pointer; }
  tbody tr:hover { background: #eef5ff; }
  tbody tr.sel { background: #d6e7ff; }
  td { padding: 3px 6px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
  td.from { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.subj { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.reasons { font-size: 10.5px; color: #444; }
  td.snip { color: #555; max-width: 460px; }
  mark { background: #fff0a8; padding: 0 1px; }
  .badge { display: inline-block; padding: 1px 5px; margin: 0 2px 2px 0; background: #e8e8ec; border-radius: 3px; font-size: 10px; }
  .badge.from_self { background: #ffe4b8; }
  .badge.jpg { background: #c5e8d2; }
  .badge.kw { background: #d8d8f5; }
  .badge.keep { background: #cfe9d5; }
  .badge.defer { background: #ececf2; }
  #view { flex: 1; overflow: auto; padding: 12px; background: #fff; }
  .hdrs { font-size: 12px; margin-bottom: 8px; padding: 8px; background: #f3f3f7; border-radius: 4px; }
  .hdrs div { margin: 1px 0; }
  .hdrs b { display: inline-block; min-width: 60px; color: #555; }
  pre.body { white-space: pre-wrap; word-break: break-word; font: 12px/1.45 ui-monospace, monospace; padding: 8px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; }
  .att { padding: 4px 8px; background: #fff8dc; border: 1px solid #e9d97f; border-radius: 4px; margin: 4px 0; font-size: 12px; }
  .toggle { float: right; }
  iframe.htmlbody { width: 100%; min-height: 60vh; border: 1px solid #ddd; border-radius: 4px; background: #fff; }
  .empty { color: #999; font-style: italic; padding: 32px; text-align: center; }
</style>
</head><body>
<header>
  <h1>Email triage search</h1>
  <div class="meta"><span id="count"></span> &nbsp;·&nbsp;
    <input id="search" type="search" placeholder="search full body, subject, from, to" autofocus>
    <select id="scope">
      <option value="all">all mail</option>
      <option value="keep">keep</option>
      <option value="defer">defer</option>
      <option value="folder:Sent">Sent</option>
      <option value="folder:Inbox">Inbox</option>
      <option value="folder:export1">export1</option>
      <option value="folder:export2">export2</option>
      <option value="folder:export3">export3</option>
    </select>
    <button id="go">Search</button>
    &nbsp;·&nbsp;
    <label><input id="attonly" type="checkbox"> 📎 has attachment</label>
  </div>
</header>
<main>
  <div id="list">
    <table>
      <thead><tr>
        <th data-sort="date">Date</th>
        <th data-sort="from">From</th>
        <th data-sort="subject">Subject</th>
        <th data-sort="att_count" title="Attachment count">📎</th>
        <th>Match</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div id="view"><div class="empty">Select a message →</div></div>
</main>
<script>
let RECORDS = [];
let FILTER = "";
let ATT_ONLY = false;
let SORT = { key: "date", dir: -1 };
let SEL = null;
let MODE = "keep";

const $rows = document.getElementById("rows");
const $count = document.getElementById("count");
const $view = document.getElementById("view");
const $search = document.getElementById("search");
const $scope = document.getElementById("scope");

function reasonBadges(reasons) {
  return reasons.map(r => {
    const cls = r === "from_self" ? "from_self" : (r === "jpg" ? "jpg" : "kw");
    return `<span class="badge ${cls}">${r}</span>`;
  }).join("");
}

function fmtDate(d) {
  if (!d) return "";
  const m = d.match(/(\\d{1,2}\\s+\\w{3}\\s+\\d{2,4})/);
  return m ? m[1] : d.slice(0, 16);
}

function dateKey(d) {
  if (!d) return 0;
  const t = Date.parse(d);
  return isNaN(t) ? 0 : t;
}

function render() {
  const q = FILTER.toLowerCase().trim();
  let rows = RECORDS;
  if (ATT_ONLY) {
    rows = rows.filter(r => (r.att_count || 0) > 0);
  }
  if (q) {
    rows = rows.filter(r => {
      const hay = [r.subject || "", r.from || "", (r.reasons || []).join(" ")].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }
  rows = rows.slice().sort((a, b) => {
    let av, bv;
    if (SORT.key === "date") { av = dateKey(a.date); bv = dateKey(b.date); }
    else if (SORT.key === "att_count") { av = a.att_count || 0; bv = b.att_count || 0; }
    else { av = (a[SORT.key] || "").toLowerCase(); bv = (b[SORT.key] || "").toLowerCase(); }
    if (av < bv) return -SORT.dir;
    if (av > bv) return SORT.dir;
    return 0;
  });
  $count.textContent = MODE === "search"
    ? `${rows.length.toLocaleString()} shown`
    : `${rows.length.toLocaleString()} of ${RECORDS.length.toLocaleString()} keep`;
  const html = rows.slice(0, 5000).map(r => `
    <tr data-key="${rowKey(r)}" class="${SEL === rowKey(r) ? "sel" : ""}">
      <td>${fmtDate(r.date)}</td>
      <td class="from">${escapeHtml(r.from || "")}</td>
      <td class="subj">${escapeHtml(r.subject || "(no subject)")}</td>
      <td style="text-align:center; color:${r.att_count ? "#a36c00" : "#ccc"}">${r.att_count ? "📎 " + r.att_count : ""}</td>
      <td class="${r.snippet ? "snip" : "reasons"}">${resultDetail(r)}</td>
    </tr>`).join("");
  $rows.innerHTML = html;
  if (rows.length > 5000) {
    $rows.innerHTML += `<tr><td colspan="4" style="text-align:center; color:#888; padding:8px;">… ${(rows.length - 5000).toLocaleString()} more (refine filter)</td></tr>`;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
}

function rowKey(r) {
  return r.path ? `path:${r.path}` : `keep:${r._id}`;
}

function resultDetail(r) {
  const status = r.triage_status ? `<span class="badge ${r.triage_status}">${r.triage_status}</span>` : "";
  const reasons = reasonBadges(r.reasons || r.triage_reasons || []);
  const snippet = safeSnippet(r.snippet || "");
  return status + reasons + snippet;
}

function safeSnippet(s) {
  return escapeHtml(s)
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

document.addEventListener("input", e => {
  if (e.target.id === "search" && MODE === "keep") { FILTER = e.target.value; render(); }
});
document.addEventListener("change", e => {
  if (e.target.id === "attonly") { ATT_ONLY = e.target.checked; render(); }
});

document.getElementById("go").addEventListener("click", runSearch);
$search.addEventListener("keydown", e => {
  if (e.key === "Enter") runSearch();
  if (e.key === "Escape") loadKeep();
});

document.querySelectorAll("th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const k = th.dataset.sort;
    if (SORT.key === k) SORT.dir *= -1; else { SORT.key = k; SORT.dir = 1; }
    render();
  });
});

$rows.addEventListener("click", async e => {
  const tr = e.target.closest("tr[data-key]");
  if (!tr) return;
  SEL = tr.dataset.key;
  document.querySelectorAll("tr.sel").forEach(t => t.classList.remove("sel"));
  tr.classList.add("sel");
  $view.innerHTML = `<div class="empty">loading…</div>`;
  const rec = RECORDS.find(r => rowKey(r) === SEL);
  const res = rec && rec.path
    ? await fetch(`/api/eml-by-path?path=${encodeURIComponent(rec.path)}`)
    : await fetch(`/api/eml/${SEL.replace("keep:", "")}`);
  if (!res.ok) { $view.innerHTML = `<div class="empty">load failed: ${res.status}</div>`; return; }
  const data = await res.json();
  renderEml(data);
});

function renderEml(d) {
  const h = d.headers;
  let bodyHtml = "";
  if (d.text_body) {
    bodyHtml += `<pre class="body">${escapeHtml(d.text_body)}</pre>`;
  }
  if (d.html_body) {
    const blob = new Blob([d.html_body], {type: "text/html"});
    const url = URL.createObjectURL(blob);
    bodyHtml += `<details ${d.text_body ? "" : "open"}><summary>HTML body</summary>
      <iframe class="htmlbody" sandbox src="${url}"></iframe></details>`;
  }
  if (!d.text_body && !d.html_body) {
    bodyHtml = `<div class="empty">(no text or html body)</div>`;
  }
  let attHtml = "";
  if (d.attachments && d.attachments.length) {
    attHtml = "<h4>Attachments</h4>" + d.attachments.map(a => {
      const byPath = d.path ? `?path=${encodeURIComponent(d.path)}` : "";
      const dl  = d.path ? `/api/att-by-path/${a.part}${byPath}` : `/api/eml/${SEL.replace("keep:", "")}/att/${a.part}`;
      const view = d.path ? `/api/att-by-path/${a.part}${byPath}&inline=1` : `/api/eml/${SEL.replace("keep:", "")}/att/${a.part}?inline=1`;
      const isImage = (a.content_type || "").startsWith("image/");
      const isPdf = a.content_type === "application/pdf";
      const isText = (a.content_type || "").startsWith("text/");
      const canInline = isImage || isPdf || isText;
      let preview = "";
      if (isImage) {
        preview = `<div style="margin-top:6px"><img src="${view}" style="max-width:100%; max-height:60vh; border:1px solid #ddd; border-radius:4px;"></div>`;
      } else if (isPdf) {
        preview = `<div style="margin-top:6px"><iframe src="${view}" style="width:100%; height:60vh; border:1px solid #ddd; border-radius:4px;"></iframe></div>`;
      }
      return `<div class="att">
        <strong>${escapeHtml(a.filename)}</strong>
        <span style="color:#888">(${a.content_type}, ${(a.size/1024).toFixed(1)} KB)</span>
        <span style="float:right">
          <a href="${dl}" download="${escapeHtml(a.filename)}">download</a>
          ${canInline ? `&nbsp;·&nbsp;<a href="${view}" target="_blank">open</a>` : ""}
        </span>
        ${preview}
      </div>`;
    }).join("");
  }
  $view.innerHTML = `
    <div class="hdrs">
      <div><b>From:</b> ${escapeHtml(h.From)}</div>
      <div><b>To:</b> ${escapeHtml(h.To)}</div>
      ${h.Cc ? `<div><b>Cc:</b> ${escapeHtml(h.Cc)}</div>` : ""}
      <div><b>Date:</b> ${escapeHtml(h.Date)}</div>
      <div><b>Subject:</b> <strong>${escapeHtml(h.Subject)}</strong></div>
    </div>
    ${bodyHtml}
    ${attHtml}
  `;
}

fetch("/api/list").then(r => r.json()).then(data => {
  RECORDS = data;
  render();
});

async function runSearch() {
  const q = $search.value.trim();
  if (!q) { loadKeep(); return; }
  MODE = "search";
  FILTER = "";
  $view.innerHTML = `<div class="empty">Searching…</div>`;
  $count.textContent = "searching";
  const url = `/api/search?q=${encodeURIComponent(q)}&scope=${encodeURIComponent($scope.value)}&limit=300`;
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) {
    $view.innerHTML = `<div class="empty">search failed: ${escapeHtml(data.error || res.status)}</div>`;
    return;
  }
  RECORDS = data.results.map(r => ({
    path: r.path,
    from: r.from,
    subject: r.subject,
    date: r.date,
    att_count: r.attachment_count,
    snippet: r.snippet,
    triage_status: r.triage_status,
    triage_reasons: r.triage_reasons,
  }));
  render();
  $count.textContent = `${RECORDS.length.toLocaleString()} of ${data.total.toLocaleString()} matches`;
  $view.innerHTML = `<div class="empty">Select a search result →</div>`;
}

async function loadKeep() {
  MODE = "keep";
  FILTER = $search.value.trim();
  const res = await fetch("/api/list");
  RECORDS = await res.json();
  SEL = null;
  render();
  $view.innerHTML = `<div class="empty">Select a message →</div>`;
}
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # quieter logs
        sys.stderr.write(f"[viewer] {fmt % args}\n")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_body, status=200):
        body = html_body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._send_html(INDEX_HTML)
            return
        if u.path == "/api/list":
            payload = [
                {
                    "_id": r["_id"],
                    "from": r.get("from", ""),
                    "subject": r.get("subject", ""),
                    "date": r.get("date", ""),
                    "reasons": r.get("reasons", []),
                    "att_count": r.get("att_count", 0),
                }
                for r in KEEP
            ]
            self._send_json(payload)
            return
        # FTS5 search endpoint
        if u.path == "/api/search":
            qs = urllib.parse.parse_qs(u.query)
            q = qs.get("q", [""])[0]
            scope = qs.get("scope", ["all"])[0]
            raw = qs.get("raw", ["0"])[0] == "1"
            try:
                limit = min(max(int(qs.get("limit", ["200"])[0]), 1), 500)
                offset = max(int(qs.get("offset", ["0"])[0]), 0)
            except ValueError:
                self._send_json({"error": "bad limit or offset"}, status=400)
                return
            if not q:
                self._send_json({"error": "missing q parameter"}, status=400)
                return
            try:
                result = search_emails(q, scope, limit, offset, raw=raw)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
                return
            self._send_json(result)
            return
        # Full email view by path (for search results)
        if u.path == "/api/eml-by-path":
            qs = urllib.parse.parse_qs(u.query)
            path_str = qs.get("path", [""])[0]
            if not path_str:
                self._send_json({"error": "missing path parameter"}, status=400)
                return
            try:
                p = safe_path(path_str)
            except ValueError:
                self._send_json({"error": "path outside root"}, status=400)
                return
            if not p.exists():
                self._send_json({"error": "file not found"}, status=404)
                return
            try:
                payload = render_eml(p)
                payload["path"] = path_str
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
            return
        m = re.match(r"^/api/att-by-path/(\d+)$", u.path)
        if m:
            part = int(m.group(1))
            qs = urllib.parse.parse_qs(u.query)
            path_str = qs.get("path", [""])[0]
            inline = qs.get("inline", ["0"])[0] == "1"
            if not path_str:
                self._send_json({"error": "missing path parameter"}, status=400)
                return
            try:
                p = safe_path(path_str)
                send_attachment(self, p, part, inline)
            except ValueError:
                self._send_json({"error": "path outside root"}, status=400)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
            return
        m = re.match(r"^/api/eml/(\d+)$", u.path)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(KEEP):
                rec = KEEP[idx]
                try:
                    p = safe_path(rec["path"])
                except ValueError:
                    self._send_json({"error": "path outside root"}, status=400)
                    return
                if not p.exists():
                    self._send_json({"error": "file not found"}, status=404)
                    return
                try:
                    payload = render_eml(p)
                    self._send_json(payload)
                except Exception as e:
                    self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
                return
            self._send_json({"error": "bad index"}, status=404)
            return
        m = re.match(r"^/api/eml/(\d+)/att/(\d+)$", u.path)
        if m:
            idx = int(m.group(1))
            part = int(m.group(2))
            qs = urllib.parse.parse_qs(u.query)
            inline = qs.get("inline", ["0"])[0] == "1"
            if not (0 <= idx < len(KEEP)):
                self._send_json({"error": "bad index"}, status=404); return
            rec = KEEP[idx]
            try:
                p = safe_path(rec["path"])
            except ValueError:
                self._send_json({"error": "path outside root"}, status=400); return
            try:
                send_attachment(self, p, part, inline)
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500); return
            return
        self.send_response(404)
        self.end_headers()


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"[viewer] serving {len(KEEP)} keep records at {url}")
    print("[viewer] Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[viewer] bye")
        server.shutdown()


if __name__ == "__main__":
    main()
