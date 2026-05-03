# AI Agent Large Email Export and Search

A complete pipeline for exporting large email archives (100K+ messages) from Yahoo Mail and performing AI-powered triage using local LLMs.
Utilizes an AI agent such as Openclaw or Hermes to manage the process of downloading massive (100k+) email storage, sorting them emails,
and delivering a curated list of high value emails for later archiving.

This method uses a cheap GPU and Ollama local model to do the AI-review without burning expensive cloud tokens.  Should work similarly for
Gmail and other clouod email systems.

## Overview

This project solves two hard problems:
1. **Export**: Yahoo Mail IMAP has a 10,000 message limit. This repo contains working code to export 100K+ emails.
2. **Triage**: Once exported, use local LLMs (Ollama) to vectorize and triage emails for importance.

## Configuration

Before running, edit these files to match your setup:

### 1. Email Address and Paths

In `export-scripts/purge_cycle.py` and `export-scripts/move_to_exports.py`:
```python
# EDIT THIS: Your Yahoo Mail email address
EMAIL = "your-email@yahoo.com"

# EDIT THIS: Set your base directory
BASE_DIR = Path("~/email-purge").expanduser()
```

In all `triage-pipeline/*.py` files:
```python
# EDIT THIS: Set your base directory
BASE_DIR = Path("~/email-purge").expanduser()
```

### 2. VIP Senders and Search Terms

In `triage-pipeline/pre_filter.py`, edit these sections:

```python
# EDIT THIS: Add domains/addresses that should always be kept
VIP_SENDERS = {
    # "your-employer.com",
    # "important-client.com",
}
VIP_DOMAINS = {
    # "courts.gov",
    # "your-employer.com",
}

# EDIT THIS: Add keywords specific to your needs
SEARCH_TERMS = [
    # Universities/Schools
    # "university name",
    # People (family, colleagues)
    # "firstname", "lastname",
    # Companies/Orgs
    # "company-name",
    # Locations
    # "country-name", "city-name",
]
```

### 3. Yahoo App Password

Generate an app password at https://login.yahoo.com/account/security/app-passwords

Use this password (not your regular password) in the scripts.

## Quick Start

### 1. Export from Yahoo Mail

```bash
cd export-scripts
python3 purge_cycle.py
```

This moves emails in batches to temporary folders, downloads them, and clears the folders. Repeat until complete.

See [docs/YAHOO_EXPORT_GUIDE.md](docs/YAHOO_EXPORT_GUIDE.md) for detailed instructions.

### 2. Pre-filter (Rule-based)

```bash
cd triage-pipeline
python3 pre_filter.py
```

Applies rule-based filtering:
- VIP senders (employers, courts)
- Search terms (names, companies, countries)
- Deadline keywords
- Attachment detection

Outputs: `keep.jsonl`, `drop.jsonl`, `needs_review.jsonl`

### 3. Build Vector Index

```bash
python3 build_embeddings.py
```

Creates embeddings using Ollama's `nomic-embed-text` model and stores in SQLite with sqlite-vec.

### 4. LLM Triage

```bash
python3 triage_emails.py
```

Uses local LLM (qwen3:4b-instruct) to score emails for importance (0-100) and categorize them.

### 5. Generate Report

```bash
python3 summary_report.py
```

Creates human-readable summary of triage results.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Yahoo Mail │────▶│  IMAP Export │────▶│  .eml + JSONL   │
│  (132K msgs)│     │  (Folder Rot)│     │  (metadata)     │
└─────────────┘     └──────────────┘     └─────────────────┘
                                                    │
                           ┌────────────────────────┘
                           ▼
                  ┌─────────────────┐
                  │  Pre-filter     │
                  │  (Rules-based)  │
                  └─────────────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌──────────┐   ┌──────────┐   ┌──────────────┐
    │   KEEP   │   │   DROP   │   │ NEEDS_REVIEW │
    └──────────┘   └──────────┘   └──────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │  Build Embeddings   │
                              │  (Ollama/nomic)     │
                              └─────────────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │  LLM Triage         │
                              │  (qwen3:4b)         │
                              │  Importance 0-100   │
                              └─────────────────────┘
                                          │
                                          ▼
                              ┌─────────────────────┐
                              │  Escalate to Human  │
                              │  (Important msgs)   │
                              └─────────────────────┘
```

## Requirements

- Python 3.10+
- Ollama with models:
  - `nomic-embed-text` (for embeddings)
  - `qwen3:4b-instruct-2507-q4_K_M` (for triage)
- Yahoo Mail account with app password
- ~10GB disk space for 100K emails

## Key Insights

### Yahoo IMAP Limitations

| Server | SELECT Limit | Notes |
|--------|-------------|-------|
| imap.mail.yahoo.com | 10,000 | Standard server |
| export.imap.mail.yahoo.com | 100,000 | Slow FETCH |

**Solution**: Move emails to temporary folders under 10K, download, repeat.

### Why Not Just Use the Export Server?

The export server (`export.imap.mail.yahoo.com`) supports 100K messages but:
- Single-message FETCH is extremely slow (seconds per message)
- Batch fetches hang indefinitely
- Not practical for bulk download

### The MOVE Command

The IMAP `MOVE` command is atomic and reliable:
```python
m._simple_command('MOVE', '1:1000', 'export1')
```

**Critical**: Always use `seq 1:N` because sequence numbers shift after each MOVE.

## Performance

| Phase | Rate | 100K Emails |
|-------|------|-------------|
| Export | 1K msgs / 25 sec | ~40 min (move) + ~30 hours (download) |
| Pre-filter | 1000 msgs / sec | ~2 min |
| Embeddings | 50 msgs / min | ~30 hours |
| Triage | 1 msg / 2 sec | ~50 hours |

**Total**: ~100 hours for full pipeline (mostly automated)

## License

MIT-0 - See [LICENSE](LICENSE)

## Contributing

This was built for a real 132,000-email migration. PRs welcome for:
- Parallel downloads
- Resume capability
- Support for Gmail/Outlook
- Better embedding models

## Credits

- **Export strategy**: Reverse-engineered from Yahoo IMAP behavior
- **Embeddings**: Ollama + nomic-embed-text
- **Triage**: qwen3:4b-instruct running locally on AMD RX 6600 XT

---

## ⚠️ Safety Notice

This tool performs **destructive operations** on your email account:

1. **MOVE operations**: Emails are moved from INBOX to temporary folders
2. **DELETE operations**: Emails are deleted from temporary folders after download

**Risk**: If the script fails between MOVE and download, emails may be lost.

**Mitigation**:
- Test on a small folder first
- Use Yahoo's web interface export as backup
- Monitor the process closely
- Verify message counts match before/after each cycle

**Recommendation**: Run manually first. Only use automated cron after thorough testing.

The authors are not responsible for data loss. Use at your own risk.
