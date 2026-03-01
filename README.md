# Insurance Tracker

Track all your insurance policies — health, car, term life — in one dashboard. Scans your Gmail for policy documents, extracts details using AI, and shows what's active, expiring, or expired.

## How It Works

```
Gmail (read-only) → AI Triage → PDF Download → AI Extraction → Encrypted Cache → Dashboard
```

1. **Gmail Scan** — Searches your inbox for insurance-related emails using specific terms (`health insurance`, `car insurance`, `term life`, etc.). Read-only access — no emails are modified or deleted.

2. **Local ML Triage** — A lightweight sentence-transformer model (`all-MiniLM-L6-v2`, 22MB) classifies emails locally as insurance-related or not. No API calls. Processes 148 emails in ~0.3 seconds.

3. **PDF Extraction** — Downloads PDF attachments from relevant emails and sends the extracted text to an AI model (Grok) to pull out structured policy details: provider, policy number, dates, premium, sum insured, members, etc.

4. **Deduplication** — Merges duplicate entries (e.g., same policy from renewal emails), fixes statuses based on dates, and produces a clean final list.

5. **Encrypted Caching** — Results are cached in a Turso database so subsequent refreshes are near-instant. All sensitive data is encrypted (see below).

## Privacy & Security

This is designed so you don't have to trust the server operator with your data.

### What we access
- **Gmail (read-only)** — Only email metadata and PDF attachments from insurance-related search results. Cannot read drafts, send emails, or access unrelated messages.
- **Email address & name** — For session identification only.

### What we store
| Data | Where | Encrypted? |
|------|-------|-----------|
| Triage results (relevant/not, reason) | Turso DB | No (needed for caching logic) |
| Extracted policy JSON | Turso DB | **Yes** — AES-256-GCM |
| Final policy data | Turso DB | **Yes** — AES-256-GCM |
| OAuth refresh token | Local file (`data/tokens/`) | No |
| PDF files | Local (`attachments/`) | No |

### Vault key encryption
- You provide a vault key when refreshing. It derives an AES-256 encryption key via PBKDF2 (100,000 iterations, SHA-256).
- All policy data in the database is encrypted with this key before storage.
- The vault key is **never stored** — not in the database, not in cookies, not on disk. It exists only in memory during a refresh.
- A SHA-256 hash of the key is stored to verify you entered the correct one. It cannot be reversed to recover the key.
- Without the vault key, the database contains unreadable encrypted blobs.
- If you forget your vault key, cached data cannot be recovered. You can still do a fresh refresh with a new key.

### What the AI sees
- **Triage (local)** — Email subjects and snippets are processed by a local ML model. Nothing leaves your machine.
- **Extraction (API)** — Truncated PDF text (first 15,000 chars) is sent to the Grok API for structured extraction. Your email address is not sent.

### Third-party services
| Service | What it does | What it receives |
|---------|-------------|-----------------|
| Google Gmail API | Email search & PDF download | OAuth token, search queries |
| xAI Grok API | PDF text → structured policy data | Truncated PDF text, email subjects |
| Turso | Encrypted cache storage | Encrypted blobs, triage metadata |

### Verify it yourself
This is open source. Key files to audit:
- [`services/triage_service.py`](services/triage_service.py) — Local ML triage (no API calls)
- [`services/db_service.py`](services/db_service.py) — Encryption/decryption logic (`encrypt()`, `decrypt()`, `derive_key()`)
- [`services/pipeline_service.py`](services/pipeline_service.py) — Full pipeline flow, what gets sent where
- [`services/gmail_service.py`](services/gmail_service.py) — Gmail API usage (read-only, search terms)
- [`app.py`](app.py) — Routes, OAuth scopes requested

## Setup

### Prerequisites
- Python 3.11+
- Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials (Web application type)
- xAI API key (for Grok)
- Turso database (free tier works)

### Install
```bash
pip install -r requirements.txt
```

### Configure
Create a `.env` file:
```
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
SESSION_SECRET=a-random-secret-string
XAI_API_KEY=your-xai-api-key
TURSO_DATABASE_URL=https://your-db.turso.io
TURSO_AUTH_TOKEN=your-turso-token
```

### Initialize database
```bash
python init_db.py
```

### Run
```bash
python app.py
```
Open http://localhost:8080

## Tech Stack
- **Backend:** FastAPI + Uvicorn
- **Database:** Turso (libSQL / cloud SQLite)
- **AI Triage:** sentence-transformers (`all-MiniLM-L6-v2`, 22MB, runs locally)
- **AI Extraction:** Grok API (xAI)
- **Auth:** Google OAuth 2.0
- **Frontend:** Vanilla HTML/CSS/JS (no framework)
- **Encryption:** AES-256-GCM via Python `cryptography` library

## License
MIT
