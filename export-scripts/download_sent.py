#!/usr/bin/env python3
"""
Download Yahoo Mail messages from one folder, defaulting to SENT.

This replaces the old INBOX rotation workflow with a single-folder export:
- resolve the Sent folder name
- count messages
- fetch each message by UID
- save raw .eml plus lightweight JSONL metadata

The script is resumable: existing non-empty .eml files are skipped.
"""

from __future__ import annotations

import argparse
import email
import email.header
import imaplib
import json
import os
import re
import sys
import time
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path

HOST = os.environ.get("YAHOO_IMAP_HOST", "export.imap.mail.yahoo.com")
PORT = int(os.environ.get("YAHOO_IMAP_PORT", "993"))
ACCOUNT = os.environ.get("YAHOO_EMAIL", "your-email@yahoo.com")
PASSWORD = os.environ.get("YAHOO_PASSWORD")

DEFAULT_FOLDER = os.environ.get("YAHOO_SENT_FOLDER", "Sent")
DEFAULT_LOCAL_FOLDER = os.environ.get("YAHOO_SENT_LOCAL_FOLDER", "Sent")

BASE_DIR = Path(os.environ.get("EMAIL_PURGE_DIR", "~/email-purge")).expanduser()
LOG_FILE = BASE_DIR / "logs" / "download_sent.log"
LOCK_FILE = BASE_DIR / "metadata" / "download_sent.lock"
STATE_FILE = BASE_DIR / "metadata" / "download_sent_state.json"

FETCH_CHUNK = 1000
SENT_CANDIDATES = (
    "Sent",
    "SENT",
    "Sent Mail",
    "Sent Messages",
    "[Yahoo]/Sent Mail",
)


class DownloadError(RuntimeError):
    pass


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def decode_header(value: str | None) -> str:
    if not value:
        return ""
    pieces: list[str] = []
    for part, charset in email.header.decode_header(value):
        if isinstance(part, bytes):
            try:
                pieces.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                pieces.append(part.decode("utf-8", errors="replace"))
        else:
            pieces.append(str(part))
    return " ".join(pieces).strip()


def extract_metadata(raw: bytes, folder: str, uid: int) -> dict:
    msg = email.message_from_bytes(raw)
    date_raw = msg.get("Date", "")
    try:
        date_iso = parsedate_to_datetime(date_raw).isoformat() if date_raw else ""
    except Exception:
        date_iso = date_raw[:100]

    attachments: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disposition:
                filename = part.get_filename()
                attachments.append(decode_header(filename) if filename else "")

    return {
        "folder": folder,
        "uid": uid,
        "message_id": str(msg.get("Message-ID", ""))[:300],
        "subject": decode_header(msg.get("Subject"))[:500],
        "from": decode_header(msg.get("From"))[:500],
        "to": decode_header(msg.get("To"))[:500],
        "date": date_iso,
        "has_attachments": bool(attachments),
        "attachments": attachments[:50],
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def safe_local_name(folder: str) -> str:
    name = folder.strip().strip('"')
    name = re.sub(r"[\\/]+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name.strip(" ._") or "Sent"


class Yahoo:
    def __init__(self) -> None:
        self.mail: imaplib.IMAP4_SSL | None = None
        self.selected: str | None = None

    def connect(self) -> None:
        self.close()
        self.mail = imaplib.IMAP4_SSL(HOST, PORT)
        self.mail.login(ACCOUNT, PASSWORD)
        self.selected = None

    def close(self) -> None:
        if self.mail is not None:
            with suppress(Exception):
                self.mail.logout()
        self.mail = None
        self.selected = None

    def ensure(self) -> imaplib.IMAP4_SSL:
        if self.mail is None:
            self.connect()
        assert self.mail is not None
        return self.mail

    def reconnect(self) -> None:
        previous = self.selected
        log("  Reconnecting to Yahoo IMAP")
        self.connect()
        if previous:
            self.select(previous, readonly=True)

    def select(self, folder: str, readonly: bool = True) -> int:
        m = self.ensure()
        typ, data = m.select(f'"{folder}"', readonly=readonly)
        if typ != "OK":
            raise DownloadError(f"Cannot select {folder}: {data}")
        self.selected = folder
        return int(data[0] or 0)

    def count(self, folder: str) -> int:
        m = self.ensure()
        typ, data = m.status(f'"{folder}"', "(MESSAGES)")
        if typ == "OK" and data:
            match = re.search(r"MESSAGES (\d+)", str(data))
            if match:
                return int(match.group(1))
        return 0

    def exists(self, folder: str) -> bool:
        m = self.ensure()
        typ, _ = m.status(f'"{folder}"', "(MESSAGES)")
        return typ == "OK"

    def uid_search_all(self, folder: str) -> list[bytes]:
        self.select(folder, readonly=True)
        typ, data = self.ensure().uid("search", None, "ALL")
        if typ != "OK":
            raise DownloadError(f"UID search failed in {folder}: {data}")
        return data[0].split() if data and data[0] else []

    def folders(self) -> list[tuple[str, str]]:
        typ, data = self.ensure().list()
        if typ != "OK" or not data:
            raise DownloadError(f"LIST failed: {data}")

        folders: list[tuple[str, str]] = []
        for row in data:
            text = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
            match = re.search(r"\((?P<flags>[^)]*)\)\s+\"[^\"]*\"\s+(?P<name>.+)$", text)
            if not match:
                continue
            name = match.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            folders.append((match.group("flags"), name))
        return folders


def acquire_lock(force: bool = False) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists() and not force:
        try:
            lock = json.loads(LOCK_FILE.read_text())
        except Exception:
            lock = {"raw": LOCK_FILE.read_text(errors="replace")}
        raise DownloadError(f"Lock exists at {LOCK_FILE}: {lock}. Use --force-lock if stale.")
    LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}, indent=2))


def release_lock() -> None:
    with suppress(FileNotFoundError):
        LOCK_FILE.unlink()


def resolve_folder(y: Yahoo, requested: str) -> str:
    requested = requested.strip()
    folders = y.folders()
    for flags, name in folders:
        if "\\Sent" in flags:
            return name

    candidates = [requested]
    lower = requested.lower()
    if lower == "sent":
        candidates = [requested, *SENT_CANDIDATES]

    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        if y.exists(candidate):
            return candidate

    for _, name in folders:
        if "sent" in name.lower():
            return name

    raise DownloadError(f"Could not resolve folder name for {requested!r}")


def download_folder(y: Yahoo, folder: str, local_folder: str, dry_run: bool, limit: int | None) -> tuple[int, int]:
    uids = y.uid_search_all(folder)
    total = len(uids)
    if total == 0:
        log(f"  {folder}: empty")
        return (0, 0)

    if limit is not None:
        uids = uids[:limit]

    local_name = safe_local_name(local_folder)
    folder_dir = BASE_DIR / "eml" / local_name
    meta_file = BASE_DIR / "metadata" / f"emails_{local_name}.jsonl"
    folder_dir.mkdir(parents=True, exist_ok=True)
    meta_file.parent.mkdir(parents=True, exist_ok=True)

    folder_count = y.count(folder)
    log(f"  {folder}: {folder_count} messages visible, {total} UIDs returned")
    if folder_count and total and total < folder_count:
        raise DownloadError(
            f"Search returned only {total} UIDs for {folder} but STATUS reports {folder_count}. "
            "This mailbox may be hitting an IMAP search cap."
        )
    if limit is not None:
        log(f"  Limiting this run to {len(uids)} messages")
    log(f"  Local output: {folder_dir}")
    if dry_run:
        log(f"DRY RUN: would fetch {len(uids)} messages from {folder}")
        return (0, 0)

    downloaded_uids: list[bytes] = []
    skipped_existing = 0
    errors = 0

    with meta_file.open("a", encoding="utf-8") as meta_f:
        for index, uid in enumerate(uids, start=1):
            uid_int = int(uid)
            eml_path = folder_dir / f"{uid_int}.eml"
            if eml_path.exists() and eml_path.stat().st_size > 0:
                skipped_existing += 1
                downloaded_uids.append(uid)
                continue

            try:
                typ, data = y.ensure().uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                    raise DownloadError(f"bad fetch response: {typ} {data[:1] if data else data}")
                raw = data[0][1]
                if not raw:
                    raise DownloadError("empty RFC822 payload")

                eml_path.write_bytes(raw)
                meta_f.write(json.dumps(extract_metadata(raw, folder, uid_int), ensure_ascii=False) + "\n")
                downloaded_uids.append(uid)

                if len(downloaded_uids) % 100 == 0:
                    log(f"  Downloaded/verified {len(downloaded_uids)}/{total} ({errors} errors)")
                if index % FETCH_CHUNK == 0:
                    y.reconnect()
                    y.select(folder, readonly=True)
            except Exception as e:
                errors += 1
                log(f"  Error fetching UID {uid.decode(errors='replace')}: {e}")
                if errors % 10 == 0:
                    y.reconnect()
                    y.select(folder, readonly=True)

    log(f"  Finished {folder}: {len(downloaded_uids)} downloaded/verified, {skipped_existing} skipped, {errors} errors")
    return (len(downloaded_uids), errors)


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def main() -> int:
    global HOST, BASE_DIR, LOG_FILE, LOCK_FILE, STATE_FILE

    parser = argparse.ArgumentParser(description="Download Yahoo Mail from the Sent folder")
    parser.add_argument("--folder", default=DEFAULT_FOLDER, help="mailbox folder to download")
    parser.add_argument("--dry-run", action="store_true", help="show intended actions without changing Yahoo")
    parser.add_argument("--count-only", action="store_true", help="count and resolve the folder without fetching messages")
    parser.add_argument("--force-lock", action="store_true", help="ignore a stale lock file")
    parser.add_argument("--host", default=HOST, help="IMAP host to use")
    parser.add_argument("--limit", type=int, help="download at most this many messages")
    parser.add_argument("--list-folders", action="store_true", help="list Yahoo folder names and exit")
    parser.add_argument("--local-folder", default=DEFAULT_LOCAL_FOLDER, help="local folder name under eml/")
    parser.add_argument("--output-dir", default=str(BASE_DIR), help="base directory for eml/metadata/logs")
    args = parser.parse_args()
    HOST = args.host
    BASE_DIR = Path(args.output_dir).expanduser()
    LOG_FILE = BASE_DIR / "logs" / "download_sent.log"
    LOCK_FILE = BASE_DIR / "metadata" / "download_sent.lock"
    STATE_FILE = BASE_DIR / "metadata" / "download_sent_state.json"

    state: dict = {
        "started_at": time.time(),
        "status": "running",
        "dry_run": args.dry_run,
        "folder": args.folder,
        "host": HOST,
        "local_eml_dir": str(BASE_DIR / "eml" / safe_local_name(args.local_folder)),
    }
    log("=== Sent Download ===")

    y: Yahoo | None = None
    try:
        acquire_lock(force=args.force_lock)
        y = Yahoo()
        if not PASSWORD:
            raise DownloadError("YAHOO_PASSWORD environment variable is required")
        if not ACCOUNT or ACCOUNT == "your-email@yahoo.com":
            raise DownloadError("YAHOO_EMAIL environment variable is required")

        y.connect()
        log(f"Connected to {HOST} as {ACCOUNT}")

        if args.list_folders:
            for flags, name in y.folders():
                log(f"  {name} ({flags})")
            state["status"] = "ok"
            write_state(state)
            return 0

        folder = resolve_folder(y, args.folder)
        if folder != args.folder:
            log(f"Resolved folder {args.folder!r} -> {folder!r}")
        state["resolved_folder"] = folder

        visible = y.count(folder)
        log(f"{folder}: {visible}")
        state["folder_count"] = visible
        if visible <= 0:
            log(f"{folder} empty - nothing to do")
            state["status"] = "empty"
            write_state(state)
            return 0
        if args.count_only:
            state["status"] = "ok"
            write_state(state)
            return 0

        downloaded, errors = download_folder(y, folder, args.local_folder, args.dry_run, args.limit)
        state.update({"downloaded_or_verified": downloaded, "download_errors": errors})
        state["status"] = "ok" if errors == 0 else "partial"

        log("=== Download Complete ===")
        return 0 if errors == 0 else 2
    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        log(f"ERROR: {e}")
        return 1
    finally:
        if y is not None:
            y.close()
        state["finished_at"] = time.time()
        write_state(state)
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
