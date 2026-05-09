#!/usr/bin/env python3
"""
email_indexer.py — Parse all .eml files into a canonical SQLite + FTS5 database.

Creates email_index.db with:
  - emails table: one row per message (metadata + clean body)
  - emails_fts: FTS5 virtual table over subject, from_addr, to_addrs, body_clean

Usage:
    python3 email_indexer.py [--db PATH] [--eml-dir PATH] [--rebuild]

Defaults:
    --db       ~/email-purge/email_index.db
    --eml-dir  ~/email-purge/eml
"""

import argparse
import email
import email.policy
import hashlib
import json
import os
import sqlite3
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

BASE_DIR = Path(os.environ.get("EMAIL_PURGE_DIR", "~/email-purge")).expanduser()


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

def html_to_text(html: str) -> str:
    """Strip HTML tags, return plain text."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["style", "script"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except Exception:
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# MIME body extraction
# ---------------------------------------------------------------------------

def get_body(msg) -> tuple:
    """Extract (plain_text, html_text, attachments) from a MIME message."""
    plain_parts = []
    html_parts = []
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition or part.get_filename():
                filename = part.get_filename() or "unknown"
                payload = part.get_payload(decode=True)
                if payload:
                    attachments.append({
                        "filename": filename,
                        "content_type": content_type,
                        "size": len(payload),
                    })
                continue

            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        plain_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        plain_parts.append(payload.decode("utf-8", errors="replace"))
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        html_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if content_type == "text/html":
                html_parts.append(text)
            else:
                plain_parts.append(text)

    plain = "\n".join(plain_parts).strip()
    html = "\n".join(html_parts).strip()
    return plain, html, attachments


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------

def get_header_str(msg, header: str) -> str:
    val = msg.get(header, "")
    return val.strip() if val else ""


def get_header_list(msg, header: str) -> list:
    val = msg.get_all(header, [])
    result = []
    for v in val:
        parts = [x.strip() for x in v.split(",") if x.strip()]
        result.extend(parts)
    return result


def parse_date(date_str: str) -> Optional[str]:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SQLite setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    folder TEXT,
    uid TEXT,
    message_id TEXT,
    subject TEXT,
    from_addr TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    bcc_addrs TEXT,
    date TEXT,
    date_raw TEXT,
    has_attachments INTEGER DEFAULT 0,
    attachment_count INTEGER DEFAULT 0,
    attachments TEXT,
    body_text TEXT,
    body_html TEXT,
    body_clean TEXT,
    body_size INTEGER DEFAULT 0,
    path_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date);
CREATE INDEX IF NOT EXISTS idx_emails_from ON emails(from_addr);
CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder);
CREATE INDEX IF NOT EXISTS idx_emails_has_attachments ON emails(has_attachments);
CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_emails_path_hash ON emails(path_hash);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject,
    from_addr,
    to_addrs,
    body_clean,
    content='emails',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS emails_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, from_addr, to_addrs, body_clean)
    VALUES (new.id, new.subject, new.from_addr, new.to_addrs, new.body_clean);
END;

CREATE TRIGGER IF NOT EXISTS emails_ad AFTER DELETE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_addr, to_addrs, body_clean)
    VALUES ('delete', old.id, old.subject, old.from_addr, old.to_addrs, old.body_clean);
END;

CREATE TRIGGER IF NOT EXISTS emails_au AFTER UPDATE ON emails BEGIN
    INSERT INTO emails_fts(emails_fts, rowid, subject, from_addr, to_addrs, body_clean)
    VALUES ('delete', old.id, old.subject, old.from_addr, old.to_addrs, old.body_clean);
    INSERT INTO emails_fts(rowid, subject, from_addr, to_addrs, body_clean)
    VALUES (new.id, new.subject, new.from_addr, new.to_addrs, new.body_clean);
END;
"""


def init_db(db_path: str, rebuild: bool = False) -> sqlite3.Connection:
    if rebuild and os.path.exists(db_path):
        os.remove(db_path)
        print(f"[init] Removed existing database: {db_path}")

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")

    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    conn.executescript(FTS_TRIGGERS)
    conn.commit()

    return conn


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------

def index_emails(eml_dir: str, db_path: str, rebuild: bool = False):
    eml_path = Path(eml_dir)
    if not eml_path.exists():
        print(f"Error: {eml_dir} does not exist")
        sys.exit(1)

    conn = init_db(db_path, rebuild=rebuild)

    existing = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    if existing > 0 and not rebuild:
        print(f"Database already has {existing} emails. Use --rebuild to re-index.")
        conn.close()
        return

    all_files = sorted(eml_path.rglob("*.eml"))
    total = len(all_files)
    print(f"[index] Found {total} .eml files in {eml_dir}")

    insert_sql = """
    INSERT OR IGNORE INTO emails (
        path, folder, uid, message_id, subject, from_addr,
        to_addrs, cc_addrs, bcc_addrs, date, date_raw,
        has_attachments, attachment_count, attachments,
        body_text, body_html, body_clean, body_size, path_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    start_time = time.time()
    batch = []
    indexed = 0
    errors = 0
    skipped = 0

    for i, eml_file in enumerate(all_files):
        try:
            raw = eml_file.read_bytes()
            if not raw:
                skipped += 1
                continue

            msg = email.message_from_bytes(raw, policy=email.policy.default)

            folder = eml_file.parent.name
            uid = eml_file.stem

            message_id = get_header_str(msg, "Message-ID")
            subject = get_header_str(msg, "Subject")
            from_addr = get_header_str(msg, "From")
            to_addrs = get_header_list(msg, "To")
            cc_addrs = get_header_list(msg, "Cc")
            bcc_addrs = get_header_list(msg, "Bcc")
            date_raw = get_header_str(msg, "Date")
            date = parse_date(date_raw)

            plain, html, attachments = get_body(msg)

            body_clean = plain if plain else html_to_text(html)
            if not body_clean:
                body_clean = ""

            path_hash = hashlib.sha256(str(eml_file).encode()).hexdigest()[:16]

            row = (
                str(eml_file),
                folder,
                uid,
                message_id,
                subject,
                from_addr,
                json.dumps(to_addrs),
                json.dumps(cc_addrs),
                json.dumps(bcc_addrs),
                date,
                date_raw,
                1 if attachments else 0,
                len(attachments),
                json.dumps(attachments),
                plain,
                html,
                body_clean,
                len(body_clean),
                path_hash,
            )
            batch.append(row)
            indexed += 1

            if len(batch) >= 2000:
                conn.executemany(insert_sql, batch)
                conn.commit()
                batch = []

                elapsed = time.time() - start_time
                rate = indexed / elapsed if elapsed > 0 else 0
                pct = (i + 1) / total * 100
                print(f"  [{i+1}/{total}] {pct:.1f}% -- {indexed} indexed, {errors} errors -- {rate:.0f} emails/sec", flush=True)

        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR [{eml_file}]: {e}", file=sys.stderr)
            elif errors == 6:
                print("  ... suppressing further errors ...", file=sys.stderr)

    if batch:
        conn.executemany(insert_sql, batch)
        conn.commit()

    elapsed = time.time() - start_time
    final_count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    print(f"\n[index] Done in {elapsed:.1f}s")
    print(f"  Indexed: {indexed} | Errors: {errors} | Skipped: {skipped}")
    print(f"  Database: {final_count} emails total")
    print(f"  DB size: {os.path.getsize(db_path) / 1024 / 1024:.1f} MB")

    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index .eml files into SQLite + FTS5")
    parser.add_argument("--db", default=str(BASE_DIR / "email_index.db"),
                        help="Output SQLite database path")
    parser.add_argument("--eml-dir", default=str(BASE_DIR / "eml"),
                        help="Directory containing .eml files")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild index from scratch")
    args = parser.parse_args()

    index_emails(args.eml_dir, args.db, args.rebuild)
