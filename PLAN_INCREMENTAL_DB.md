# Incremental Processing with Turso DB + Vault Key Encryption

## Problem

Every refresh re-processes ALL 148 emails (~60s) even when nothing has changed. Sensitive policy data shouldn't be readable by anyone with DB access.

## Solution

1. **Incremental processing:** Store which emails have been processed in Turso. Skip them on next refresh. Second refresh: ~3-5s.
2. **Vault key encryption:** Sensitive fields (extraction JSON, policy JSON) encrypted with a user-provided vault key using AES-256-GCM. Without the key, DB contents are gibberish.

---

## Vault Key System

**Flow:**
1. User clicks Refresh → prompted for vault key (default: `Ashish` for now)
2. Server derives AES-256 key: `PBKDF2(vault_key, salt, 100000 iterations)`
3. On first use: store `vault_hash` (to verify key on future uses) + random `vault_salt` in users table
4. On subsequent uses: verify vault_hash matches before proceeding
5. Encrypt sensitive data before DB write, decrypt on read
6. Vault key never stored — only lives in memory during the refresh request

**What's encrypted (DB is gibberish without vault key):**
- `processed_emails.extraction_json` — raw LLM extraction output
- `policies.policy_json` — final deduped policy data

**What's plaintext (readable for monitoring):**
- `processed_emails.is_relevant` — triage decision (1/0)
- `processed_emails.triage_reason` — LLM's reasoning
- `policies.policy_number_norm` — normalized policy number (needed for dedup queries)
- All timestamps

---

## Database Schema (Turso — 3 tables)

### `users`
```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    vault_hash TEXT,        -- hash of vault key (verify correct key)
    vault_salt TEXT,         -- random salt for key derivation
    created_at TEXT NOT NULL
);
```

### `processed_emails`
```sql
CREATE TABLE IF NOT EXISTS processed_emails (
    msg_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    is_relevant INTEGER NOT NULL DEFAULT 0,
    triage_reason TEXT,              -- plaintext (monitoring)
    extraction_json TEXT,            -- ENCRYPTED
    processed_at TEXT NOT NULL,
    PRIMARY KEY (msg_id, user_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### `policies`
```sql
CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    policy_number_norm TEXT,          -- plaintext (dedup lookups)
    policy_json TEXT NOT NULL,        -- ENCRYPTED
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

---

## Pipeline Flow

```
0. Vault key prompt             → user enters key (default "Ashish")
1. Verify vault key             → derive key, check vault_hash
2. Gmail metadata fetch         → 148 emails (~2s)
3. DB lookup                    → which msg_ids already processed?
4. Partition                    → new = all - known (0 on 2nd refresh)
5. Triage (new only)            → save is_relevant + reason (plaintext)
6. Extract (new relevant only)  → save extraction_json (ENCRYPTED)
7. Finalize                     → decrypt cached + merge new → dedup
8. Persist                      → encrypt → policies table + file cache
```

---

## File Changes

| File | Action | What |
|------|--------|------|
| `services/db_service.py` | NEW | Database wrapper + encryption helpers + all DB functions |
| `services/pipeline_service.py` | MODIFY | Add skip_msg_ids params, msg_id threading, DB saves |
| `app.py` | MODIFY | Init DB, vault key from query param, wire into SSE |
| `static/app.js` | MODIFY | Vault key prompt, pass to SSE URL |
| `.env` | MODIFY | Add TURSO_DATABASE_URL + TURSO_AUTH_TOKEN |
| `requirements.txt` | MODIFY | Add libsql-client, cryptography |
| `init_db.py` | NEW | Standalone table creation |

No changes: cache_service.py, gmail_service.py, index.html, style.css

---

## Security Summary

| Threat | Protected? | How |
|--------|-----------|-----|
| DB dump/breach | Yes | AES-256-GCM encryption on sensitive fields |
| Someone with only Turso dashboard access | Yes | Sees encrypted blobs |
| Server admin without vault key | Yes | Can see triage metadata but not policy details |
| Server admin with vault key | No | Has everything needed to decrypt |
| Wrong vault key entered | Caught | vault_hash verification fails → error |

---

## Expected Performance

| Scenario | Time | LLM Calls |
|----------|------|-----------|
| First refresh (cold) | ~60s | ~165 |
| Second refresh (all cached) | ~3-5s | 0 |
| 3 new emails since last refresh | ~8s | ~5 |
