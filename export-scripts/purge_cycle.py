#!/usr/bin/env python3
"""
One cycle of the email purge:
1. Move 10K messages from INBOX to next available export folder
2. Download that export folder
3. Report status
"""
import imaplib, re, time, sys, os, json
from pathlib import Path

HOST = "imap.mail.yahoo.com"
EMAIL = "kc7rcy@yahoo.com"
PASS = "zgvpnfymmwxebpof"
EXPORT_FOLDERS = ["export1", "export2", "export3"]
MOVE_BATCH = 8999  # Yahoo errors at 10000
BASE_DIR = Path("/home/jim/email-purge")
LOG_FILE = BASE_DIR / "logs" / "purge_cycle.log"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def count(m, folder):
    typ, data = m.status(f'"{folder}"', '(MESSAGES)')
    if typ == 'OK' and data:
        match = re.search(r'MESSAGES (\d+)', str(data))
        if match: return int(match.group(1))
    return 0

def move_batch(m, target, amount=MOVE_BATCH):
    """Move messages from INBOX to target folder."""
    moved = 0
    m.select('"INBOX"', readonly=False)
    for i in range(0, amount, 1000):
        chunk = min(1000, amount - moved)
        seq_range = f"1:{chunk}"
        try:
            typ, data = m._simple_command('MOVE', seq_range, target)
            if typ == 'OK':
                moved += chunk
                log(f"  Moved {chunk} -> {target} ({moved} total)")
            else:
                log(f"  MOVE failed: {typ}")
                break
            time.sleep(3)
        except Exception as e:
            log(f"  MOVE error: {e}")
            time.sleep(10)
            # reconnect
            try: m.logout()
            except: pass
            m = imaplib.IMAP4_SSL(HOST, 993)
            m.login(EMAIL, PASS)
            break
    return moved

def download_folder(m, folder):
    """Download all messages in a folder using sequence numbers."""
    m.select(f'"{folder}"', readonly=True)
    total = count(m, folder)
    if total == 0:
        return 0

    folder_dir = BASE_DIR / "eml" / folder
    folder_dir.mkdir(parents=True, exist_ok=True)
    meta_file = BASE_DIR / "metadata" / f"emails_{folder}.jsonl"

    downloaded = 0
    errors = 0

    for seq in range(1, total + 1):
        try:
            typ, data = m.fetch(str(seq), '(RFC822)')
            if typ != 'OK' or not data or not data[0]:
                errors += 1
                continue

            if isinstance(data[0], tuple):
                raw = data[0][1]
            else:
                errors += 1
                continue

            if raw is None:
                errors += 1
                continue

            # Save .eml
            eml_path = folder_dir / f"{seq}.eml"
            with open(eml_path, 'wb') as f:
                f.write(raw)

            # Extract basic metadata
            import email
            msg = email.message_from_bytes(raw)
            subj = msg.get("Subject", "")
            from_addr = msg.get("From", "")
            date_str = msg.get("Date", "")

            meta = {
                "folder": folder,
                "seq": seq,
                "subject": subj[:200] if subj else "",
                "from": from_addr[:200] if from_addr else "",
                "date": date_str[:50] if date_str else "",
            }
            with open(meta_file, "a") as f:
                f.write(json.dumps(meta) + "\n")

            downloaded += 1
            if downloaded % 100 == 0:
                log(f"  Downloaded {downloaded}/{total} ({errors} errors)")

        except Exception as e:
            errors += 1
            if errors % 50 == 0:
                log(f"  Error at seq {seq}: {e}")
            # Reconnect periodically
            if downloaded % 500 == 0 and downloaded > 0:
                try: m.logout()
                except: pass
                time.sleep(2)
                m = imaplib.IMAP4_SSL(HOST, 993)
                m.login(EMAIL, PASS)
                m.select(f'"{folder}"', readonly=True)

    log(f"  Download complete: {downloaded}/{total} ({errors} errors)")
    return downloaded

def main():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    log("=== Purge Cycle ===")

    m = imaplib.IMAP4_SSL(HOST, 993)
    m.login(EMAIL, PASS)

    inbox = count(m, "INBOX")
    log(f"INBOX: {inbox}")

    if inbox == 0:
        log("INBOX empty - nothing to do")
        m.logout()
        return

    # Find next export folder
    target = None
    for f in EXPORT_FOLDERS:
        c = count(m, f)
        if c < 10000:
            target = f
            break

    if not target:
        log("All export folders full! Need to download and clear them first.")
        m.logout()
        sys.exit(1)

    # Step 1: Move 10K to export folder
    log(f"Step 1: Moving 10K to {target}")
    moved = move_batch(m, target, 10000)
    log(f"Moved {moved} messages")

    # Step 2: Download the export folder
    log(f"Step 2: Downloading {target}")
    downloaded = download_folder(m, target)
    log(f"Downloaded {downloaded} messages")

    # Step 3: Clear the export folder (move to Trash or delete)
    log(f"Step 3: Clearing {target}")
    m.select(f'"{target}"', readonly=False)
    typ, data = m.search(None, 'ALL')
    uids = data[0].split() if data[0] else []
    if uids:
        # Delete all messages in the folder
        for i in range(0, len(uids), 1000):
            batch = uids[i:i+1000]
            seq_range = f"1:{len(batch)}"
            try:
                m.store(seq_range, '+FLAGS', '\\Deleted')
                m.expunge()
                log(f"  Deleted batch {i//1000 + 1}")
                time.sleep(2)
            except Exception as e:
                log(f"  Delete error: {e}")

    # Final status
    inbox_final = count(m, "INBOX")
    log(f"Final: INBOX={inbox_final}")
    for f in EXPORT_FOLDERS:
        c = count(m, f)
        if c > 0:
            log(f"  {f}: {c}")

    m.logout()
    log("=== Cycle Complete ===")

if __name__ == "__main__":
    main()
