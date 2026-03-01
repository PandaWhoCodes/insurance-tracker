"""Local ML triage using sentence-transformers (all-MiniLM-L6-v2, 22MB).

Replaces Grok API triage with cosine similarity against reference phrases.
~0.3s for 148 emails vs ~30s with API calls.
"""

import logging
import time

from sentence_transformers import SentenceTransformer, util

logger = logging.getLogger(__name__)

# ── Positive reference phrases (what a real policy document looks like) ──
POSITIVE_PHRASES = [
    "your insurance policy document is ready",
    "policy copy attached for your records",
    "policy schedule enclosed",
    "renewed policy document attached",
    "policy renewal certificate",
    "policy issuance confirmation with attached document",
    "insurance premium payment receipt attached",
    "premium certificate for the year",
    "premium paid confirmation for policy",
    "health insurance policy copy",
    "mediclaim policy document",
    "optima restore health policy",
    "care freedom health plan policy",
    "comprehensive car insurance policy document",
    "motor insurance certificate attached",
    "vehicle insurance policy copy",
    "term life insurance policy copy attached",
    "iprotect smart term plan document",
    "term insurance certificate of insurance",
    "policy number enclosed with document",
    "sum insured details in attached policy",
    "attached herewith your policy",
]

# ── Negative reference phrases (marketing/spam patterns) ──
NEGATIVE_PHRASES = [
    "renew your insurance policy today special offer",
    "your insurance is expiring buy now",
    "lowest premium guaranteed compare plans",
    "get health cover starting at just rupees per day",
    "exclusive insurance offer discount",
    "save on your insurance renewal",
    "urgent alert policy expired renew immediately",
    "daily trading and investment ideas newsletter",
    "weekly market update stocks and funds",
    "mutual fund investment SIP update",
    "annual general meeting notice shareholders",
    "postal ballot notice bank limited",
    "TDS certificate form 16A dividend",
    "credit card statement communication",
    "hassle free healthcare claim process",
    "need help with a claim contact us",
    "digital platforms for policy servicing",
]

# Tuned parameters (from demo_triage_v2.py sweep)
THRESHOLD = 0.25
NEG_WEIGHT = 0.3
ATTACHMENT_BOOST = 0.05


class TriageService:
    """Classify emails as insurance-related using local sentence embeddings."""

    def __init__(self):
        self._model = None
        self._pos_emb = None
        self._neg_emb = None

    def _ensure_loaded(self):
        if self._model is not None:
            return
        t0 = time.time()
        self._model = SentenceTransformer("all-MiniLM-L6-v2")
        self._pos_emb = self._model.encode(POSITIVE_PHRASES, convert_to_tensor=True)
        self._neg_emb = self._model.encode(NEGATIVE_PHRASES, convert_to_tensor=True)
        logger.info(f"Triage model loaded in {time.time() - t0:.1f}s")

    def classify_batch(
        self, email_metadata: list[dict]
    ) -> list[tuple[bool, str, float]]:
        """Classify a batch of emails.

        Args:
            email_metadata: list of dicts with 'subject', 'from', 'snippet' keys

        Returns:
            list of (is_relevant, reason, score) tuples
        """
        self._ensure_loaded()

        if not email_metadata:
            return []

        # Build text for each email
        texts = [self._build_text(m) for m in email_metadata]

        t0 = time.time()
        email_emb = self._model.encode(texts, convert_to_tensor=True, batch_size=32)

        pos_scores = util.cos_sim(email_emb, self._pos_emb).max(dim=1).values
        neg_scores = util.cos_sim(email_emb, self._neg_emb).max(dim=1).values
        elapsed = time.time() - t0

        logger.info(f"Triage: classified {len(email_metadata)} emails in {elapsed:.3f}s")

        results = []
        for i, meta in enumerate(email_metadata):
            pos = pos_scores[i].item()
            neg = neg_scores[i].item()

            combined = pos - (NEG_WEIGHT * neg)
            if self._has_attachment(meta):
                combined += ATTACHMENT_BOOST

            is_relevant = combined >= THRESHOLD

            if is_relevant:
                reason = f"similarity:{combined:.3f}"
            else:
                reason = f"below_threshold:{combined:.3f}"

            results.append((is_relevant, reason, combined))

        return results

    def _build_text(self, meta: dict) -> str:
        parts = []
        if meta.get("subject"):
            parts.append(meta["subject"])
        if meta.get("from"):
            parts.append(f"From: {meta['from']}")
        if meta.get("snippet"):
            parts.append(meta["snippet"][:200])
        return " | ".join(parts)

    def _has_attachment(self, meta: dict) -> bool:
        """Check for attachment signals in metadata."""
        # SSE pipeline metadata has 'has_attachments' flag
        if meta.get("has_attachments"):
            return True
        # Local JSON data has 'attachments' list
        attachments = meta.get("attachments", [])
        if not attachments:
            return bool(meta.get("pdf_texts"))
        for att in attachments:
            if isinstance(att, str) and att.lower().endswith(".pdf"):
                return True
            if isinstance(att, dict):
                name = (att.get("filename") or "").lower()
                if name.endswith(".pdf"):
                    return True
        return False
