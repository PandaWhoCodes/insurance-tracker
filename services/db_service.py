import os
import json
import hashlib
import logging
from base64 import b64encode, b64decode
from datetime import datetime

# Fix macOS Python SSL cert issue (must be set before aiohttp imports)
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

import libsql_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        name TEXT,
        vault_hash TEXT,
        vault_salt TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS processed_emails (
        msg_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        is_relevant INTEGER NOT NULL DEFAULT 0,
        triage_reason TEXT,
        extraction_json TEXT,
        processed_at TEXT NOT NULL,
        PRIMARY KEY (msg_id, user_id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS policies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        policy_number_norm TEXT,
        policy_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pe_user ON processed_emails(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_pol_user ON policies(user_id)",
]

# ── Encryption helpers ────────────────────────────


def derive_key(vault_key: str, salt: str) -> bytes:
    """PBKDF2 derive a 32-byte AES-256 key from vault_key + salt."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt.encode("utf-8"),
        iterations=100_000,
    )
    return kdf.derive(vault_key.encode("utf-8"))


def encrypt(plaintext: str, key: bytes) -> str:
    """AES-256-GCM encrypt. Returns base64(nonce + ciphertext)."""
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return b64encode(nonce + ct).decode("ascii")


def decrypt(ciphertext_b64: str, key: bytes) -> str:
    """AES-256-GCM decrypt. Input is base64(nonce + ciphertext)."""
    raw = b64decode(ciphertext_b64)
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def _hash_vault_key(vault_key: str, salt: str) -> str:
    """Simple SHA-256 hash for vault key verification (not encryption)."""
    return hashlib.sha256((vault_key + salt).encode("utf-8")).hexdigest()


# ── Database class ────────────────────────────────


class Database:
    def __init__(self):
        self._client = None

    async def connect(self):
        url = os.getenv("TURSO_DATABASE_URL", "")
        token = os.getenv("TURSO_AUTH_TOKEN", "")
        if not url:
            raise RuntimeError("TURSO_DATABASE_URL not set in .env")
        self._client = libsql_client.create_client(url=url, auth_token=token or None)
        logger.info("Connected to Turso DB")

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None

    async def execute(self, sql: str, args=None):
        return await self._client.execute(sql, args or [])

    async def query(self, sql: str, args=None):
        rs = await self._client.execute(sql, args or [])
        return [dict(zip(rs.columns, row)) for row in rs.rows]

    async def query_one(self, sql: str, args=None):
        rows = await self.query(sql, args)
        return rows[0] if rows else None

    async def init_schema(self):
        await self._client.batch(SCHEMA_SQL)
        logger.info("DB schema initialized")


# ── Global singleton ──────────────────────────────
db = Database()


# ── Domain functions ──────────────────────────────


async def get_or_create_user(email: str, name: str = None) -> int:
    """Get existing user or create one. Returns user_id."""
    row = await db.query_one("SELECT id FROM users WHERE email = ?", [email])
    if row:
        return row["id"]
    now = datetime.now().isoformat()
    # Generate a random salt for this user
    vault_salt = b64encode(os.urandom(16)).decode("ascii")
    rs = await db.execute(
        "INSERT INTO users (email, name, vault_salt, created_at) VALUES (?, ?, ?, ?)",
        [email, name, vault_salt, now],
    )
    logger.info(f"Created user {email} with id {rs.last_insert_rowid}")
    return rs.last_insert_rowid


async def get_user(user_id: int) -> dict | None:
    return await db.query_one("SELECT * FROM users WHERE id = ?", [user_id])


async def verify_vault_key(user_id: int, vault_key: str) -> bytes:
    """Verify vault key (or set it on first use). Returns derived AES key.
    Raises ValueError if key is wrong."""
    user = await get_user(user_id)
    if not user:
        raise ValueError("User not found")
    salt = user["vault_salt"]
    key_hash = _hash_vault_key(vault_key, salt)

    if user["vault_hash"] is None:
        # First use — store the hash
        await db.execute(
            "UPDATE users SET vault_hash = ? WHERE id = ?", [key_hash, user_id]
        )
        logger.info(f"Vault key set for user {user_id}")
    elif user["vault_hash"] != key_hash:
        raise ValueError("Wrong vault key")

    return derive_key(vault_key, salt)


async def get_processed_msg_ids(user_id: int) -> set[str]:
    """Get all msg_ids already processed for this user."""
    rows = await db.query(
        "SELECT msg_id FROM processed_emails WHERE user_id = ?", [user_id]
    )
    return {r["msg_id"] for r in rows}


async def save_triage_result(msg_id: str, user_id: int, is_relevant: bool, reason: str):
    """Save triage result (plaintext — no encryption needed)."""
    now = datetime.now().isoformat()
    await db.execute(
        """INSERT OR REPLACE INTO processed_emails
           (msg_id, user_id, is_relevant, triage_reason, processed_at)
           VALUES (?, ?, ?, ?, ?)""",
        [msg_id, user_id, int(is_relevant), reason, now],
    )


async def save_extraction_result(
    msg_id: str, user_id: int, extraction_json: dict, key: bytes
):
    """Encrypt and save extraction JSON for a processed email."""
    encrypted = encrypt(json.dumps(extraction_json), key)
    now = datetime.now().isoformat()
    await db.execute(
        """UPDATE processed_emails
           SET extraction_json = ?, processed_at = ?
           WHERE msg_id = ? AND user_id = ?""",
        [encrypted, now, msg_id, user_id],
    )


async def get_cached_extractions(user_id: int, key: bytes) -> list[dict]:
    """Load and decrypt all extraction_json for relevant emails."""
    rows = await db.query(
        """SELECT msg_id, extraction_json FROM processed_emails
           WHERE user_id = ? AND is_relevant = 1 AND extraction_json IS NOT NULL""",
        [user_id],
    )
    results = []
    for r in rows:
        try:
            plaintext = decrypt(r["extraction_json"], key)
            data = json.loads(plaintext)
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
        except Exception as e:
            logger.warning(f"Failed to decrypt extraction for msg {r['msg_id']}: {e}")
    return results


async def save_final_policies(user_id: int, policies: list[dict], key: bytes):
    """Replace all policies for user with encrypted versions."""
    # Delete existing
    await db.execute("DELETE FROM policies WHERE user_id = ?", [user_id])
    now = datetime.now().isoformat()
    for p in policies:
        pn = p.get("policy_number") or ""
        encrypted = encrypt(json.dumps(p), key)
        await db.execute(
            """INSERT INTO policies (user_id, policy_number_norm, policy_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            [user_id, pn, encrypted, now],
        )
