"""Triage service: classify emails as insurance-related.

Primary: Groq LLM (Llama 3.1 8B) — fast, cheap, accurate, batched.
Fallback: keyword-based scoring if Groq unavailable.
"""

import asyncio
import logging
import os
import re
import time

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

TRIAGE_PROMPT = """You are classifying emails that have PDF attachments to find insurance policy documents.
All these emails have attachments. Decide if the attachment is likely an insurance policy PDF.

Mark as YES — the PDF attachment is likely an insurance policy document:
- Policy documents, policy copies, policy certificates, policy schedules
- Renewed policy documents ("Your Renewed Policy Document")
- Updated policy documents ("Updated Insurance Policy")
- Premium receipts or premium paid certificates
- Certificate of insurance documents
- "Email Policy Copy" or "Your policy document is here" emails
- "Thank you for choosing [insurer]" or "Congratulations" emails with policy PDFs
- "Communication for Policy no ..." emails from insurers
- Forwarded (Fwd:) versions of any of the above
- Non-registration letters or underwriting letters referencing a policy number

Mark as NO — the PDF attachment is NOT an insurance policy:
- Newsletters, market reports, mutual fund updates with PDF reports
- Bank statements, credit card statements, loan documents
- Tax certificates, TDS certificates (Form 16A), interest certificates
- Dividend notices (IndianOil, etc.)
- Travel booking confirmations (MakeMyTrip, AirAsia, etc.)
- Non-insurance PDFs (invoices, receipts from non-insurers, etc.)

Respond with ONLY numbered YES/NO:
1. YES
2. NO
...

One line per email, matching the numbering."""

BATCH_SIZE = 30  # emails per LLM call


class TriageService:
    """Classify emails as insurance-related using Groq LLM or keyword fallback."""

    def __init__(self):
        self._client = None
        self._model = None
        self._init_groq()

    def _init_groq(self):
        api_key = os.getenv("GROQ_API_KEY")
        if api_key:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1",
            )
            self._model = "llama-3.1-8b-instant"
            logger.info("Triage: using Groq (Llama 3.1 8B)")
        else:
            logger.warning("Triage: GROQ_API_KEY not set, using keyword fallback")

    def _format_email(self, idx: int, meta: dict) -> str:
        subject = meta.get("subject", "(no subject)")
        sender = meta.get("from", "")
        snippet = (meta.get("snippet") or "")[:150]
        has_att = "Yes" if self._has_attachment(meta) else "No"
        return f"{idx}. Subject: {subject}\n   From: {sender}\n   Snippet: {snippet}\n   Has attachment: {has_att}"

    async def classify_batch_async(
        self, email_metadata: list[dict]
    ) -> list[tuple[bool, str, float]]:
        """Classify emails using Groq LLM in batches. Falls back to keyword.
        Pre-filters: emails without PDF attachments are auto-rejected.
        """
        if not email_metadata:
            return []

        # Pre-filter: only send emails WITH attachments to LLM
        results = [None] * len(email_metadata)
        llm_indices = []  # indices of emails that need LLM classification
        skipped = 0

        for i, meta in enumerate(email_metadata):
            if not meta.get("has_attachments"):
                results[i] = (False, "no_attachment", 0.0)
                skipped += 1
            else:
                llm_indices.append(i)

        logger.info(f"Triage pre-filter: {skipped} skipped (no attachment), {len(llm_indices)} sent to LLM")

        if not llm_indices:
            return results

        # Build list of emails that need LLM classification
        llm_emails = [email_metadata[i] for i in llm_indices]

        if not self._client:
            for idx, meta in zip(llm_indices, llm_emails):
                results[idx] = self._keyword_classify(meta)
            return results

        batches = []
        for i in range(0, len(llm_emails), BATCH_SIZE):
            batches.append((i, llm_emails[i:i + BATCH_SIZE]))

        t0 = time.time()
        tasks = [self._classify_one_batch(start, batch) for start, batch in batches]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for (start, batch), batch_res in zip(batches, batch_results):
            if isinstance(batch_res, Exception):
                logger.warning(f"Groq triage batch failed: {batch_res}, using keyword fallback")
                for j, meta in enumerate(batch):
                    orig_idx = llm_indices[start + j]
                    results[orig_idx] = self._keyword_classify(meta)
            else:
                for j, res in enumerate(batch_res):
                    orig_idx = llm_indices[start + j]
                    results[orig_idx] = res

        elapsed = time.time() - t0
        logger.info(f"Triage: classified {len(llm_emails)} emails via Groq in {elapsed:.2f}s ({len(batches)} batches), {skipped} pre-filtered")
        return results

    async def _classify_one_batch(
        self, start_idx: int, batch: list[dict]
    ) -> list[tuple[bool, str, float]]:
        """Send one batch to Groq and parse YES/NO responses."""
        email_lines = "\n".join(
            self._format_email(i + 1, m) for i, m in enumerate(batch)
        )
        user_msg = f"Classify these {len(batch)} emails:\n\n{email_lines}"

        t0 = time.time()
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": TRIAGE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=len(batch) * 5,  # ~4 chars per "YES\n" or "NO\n"
        )
        elapsed = time.time() - t0
        usage = response.usage
        tokens_info = f"in={usage.prompt_tokens},out={usage.completion_tokens}" if usage else ""
        logger.info(f"[Timing] Triage Groq batch: {elapsed:.2f}s ({tokens_info}) — {len(batch)} emails")

        content = response.choices[0].message.content.strip()

        # Parse numbered responses: "1. YES", "2. NO", etc.
        parsed = {}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"(\d+)\.\s*(YES|NO)", line, re.IGNORECASE)
            if m:
                num = int(m.group(1))
                parsed[num] = m.group(2).upper() == "YES"

        results = []
        for i, meta in enumerate(batch):
            num = i + 1  # 1-indexed
            if num in parsed:
                is_yes = parsed[num]
                results.append((is_yes, f"groq:{'yes' if is_yes else 'no'}", 1.0 if is_yes else 0.0))
            else:
                # Missing number — fall back to keyword
                results.append(self._keyword_classify(meta))

        return results

    # ── Keyword-based fallback ─────────────────

    _STRONG_POS = [
        "policy document", "policy copy", "policy schedule", "policy certificate",
        "renewed policy", "policy renewal", "certificate of insurance",
        "premium receipt", "premium payment", "premium paid",
        "policy issuance", "policy bond", "policy dispatch",
        "your policy", "policy attached", "policy enclosed",
        "sum insured", "insured members",
        "thank you for choosing", "you are now secured with",
    ]

    _WEAK_POS = [
        "health insurance", "car insurance", "motor insurance", "vehicle insurance",
        "term insurance", "term life", "term plan", "life insurance",
        "mediclaim", "travel insurance", "home insurance", "bike insurance",
        "two wheeler", "comprehensive cover", "insurance policy",
    ]

    _NEGATIVE = [
        "renew now", "buy now", "compare plans", "special offer", "discount",
        "lowest premium", "save up to", "limited period", "offer valid",
        "get a quote", "check premium", "calculate premium",
        "claim process", "claim settlement", "claim form",
        "how to file", "how to claim", "hassle free",
        "need help", "contact us", "customer care",
        "newsletter", "weekly update", "daily digest",
        "trading", "investment", "mutual fund", "sip",
        "demat", "stocks", "portfolio", "nifty", "sensex",
        "credit card", "loan", "emi", "bank statement",
        "tds certificate", "form 16", "itr",
        "shareholder", "annual general meeting", "postal ballot",
        "unsubscribe from", "view in browser",
    ]

    _INSURER_SENDERS = [
        "hdfc ergo", "hdfcergo", "icici lombard", "icicilombard",
        "icici prudential", "icicipru", "bajaj allianz", "bajajallianz",
        "care health", "carehealth", "careinsurance", "star health", "starhealth",
        "tata aig", "tataaig", "new india", "oriental", "acko",
        "lic", "max life", "maxlife", "sbi life", "sbigeneral",
        "policybazaar", "tacterial", "niva bupa", "nivabupa",
        "digit insurance", "godigit", "chola ms", "cholams",
        "reliance general", "future generali", "kotak life",
    ]

    def _keyword_classify(self, meta: dict) -> tuple[bool, str, float]:
        """Classify a single email using keyword matching (no ML)."""
        subject = (meta.get("subject") or "").lower()
        sender = (meta.get("from") or "").lower()
        snippet = (meta.get("snippet") or "").lower()[:200]
        text = f"{subject} {snippet}"

        score = 0.0

        for kw in self._STRONG_POS:
            if kw in subject:
                score += 0.4
                break

        for kw in self._WEAK_POS:
            if kw in subject:
                score += 0.15
                break

        for kw in self._STRONG_POS:
            if kw in snippet:
                score += 0.15
                break

        for ins in self._INSURER_SENDERS:
            if ins in sender:
                score += 0.2
                break

        if self._has_attachment(meta):
            score += 0.15

        neg_count = 0
        for kw in self._NEGATIVE:
            if kw in text:
                neg_count += 1
        score -= neg_count * 0.15

        is_relevant = score >= 0.3
        reason = f"keyword:{score:.2f}" if is_relevant else f"keyword_below:{score:.2f}"
        return (is_relevant, reason, score)

    def _has_attachment(self, meta: dict) -> bool:
        """Check for attachment signals in metadata."""
        if meta.get("has_attachments"):
            return True
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
