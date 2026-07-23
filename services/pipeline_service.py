import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

from openai import AsyncOpenAI

from services import db_service
from services.triage_service import TriageService

logger = logging.getLogger(__name__)

EXTRACT_CONCURRENCY = 3

# Check if Modal is available
try:
    import modal  # noqa: F401
    MODAL_AVAILABLE = True
except ImportError:
    MODAL_AVAILABLE = False

# LIC plan table number → human-readable name mapping
LIC_PLAN_NAMES = {
    "814": "Jeevan Anand (Endowment)",
    "820": "Jeevan Umang (Whole Life)",
    "836": "Jeevan Saral (Endowment)",
    "842": "Jeevan Labh",
    "843": "Jeevan Lakshya",
    "844": "Jeevan Pragati",
    "845": "Jeevan Shiromani",
    "849": "Jeevan Azad",
    "914": "New Endowment Plan",
    "935": "Nivesh Plus (ULIP)",
    "936": "Bima Jyoti (Savings)",
    "941": "Dhan Sanchay (Savings)",
    "945": "Jeevan Amar (Term Life)",
    "946": "SIIP (ULIP)",
    "954": "Bima Ratna (Endowment)",
    "955": "Amritbaal (Children)",
    "956": "Jeevan Utsav (Whole Life)",
}

EXTRACT_PROMPT = """You are an expert insurance document analyzer. Given extracted text from an insurance policy PDF or email, extract structured policy information.

Return a JSON object with these fields:

{
    "policy_number": "string",
    "type": "health" | "car" | "term_life" | "travel" | "home",
    "provider": "Insurance company name",
    "plan_name": "Plan/product name",
    "insured_members": [
        {
            "name": "Full name",
            "relationship": "Self | Father | Mother | Spouse | Child | Brother",
            "date_of_birth": "YYYY-MM-DD or null"
        }
    ],
    "sum_insured": number or null,
    "premium": number or null,
    "premium_frequency": "yearly | monthly | one_time | quarterly | single",
    "policy_start": "YYYY-MM-DD or null",
    "policy_end": "YYYY-MM-DD or null",
    "policy_term": number of years (integer) or null,
    "status": "ACTIVE | EXPIRED | UNKNOWN",
    "vehicle": {"make": "string", "model": "string", "registration": "string"} or null,
    "nominee": {"name": "string", "relationship": "string"} or null,
    "intermediary": "Agent/broker name or null",
    "coverages": ["list of key coverages/add-ons"] or null,
    "notes": "Any important details — add-ons, special conditions, pre-existing conditions declared"
}

RULES:
- Extract ONLY what is explicitly stated in the document.
- For amounts, return numbers without currency symbols (e.g., 1500000 not "Rs 15,00,000").
- Dates must be YYYY-MM-DD format.
- If a field is not found, set it to null.
- For health insurance, list all insured members with relationships.
- For car insurance, include vehicle details.
- For term life, include nominee details and policy term.
- Determine status: if policy_end date is in the past, mark EXPIRED. If in the future, ACTIVE. Otherwise UNKNOWN.
- If the document is not an insurance policy (e.g., marketing email, newsletter, bank statement), return exactly: {"skip": true}

IMPORTANT — DATES & TERM:
- If the document contains multiple policy periods (e.g., original and renewal), always use the LATEST policy period dates.
- For multi-year policies (e.g., 2-year or 3-year), set policy_end to the FINAL expiry date, not an intermediate year.
- Look carefully for "Policy Period", "Period of Insurance", "Risk Start Date / End Date" fields.
- Extract the policy term in years if available (e.g., "Policy Term: 34 years" -> 34).

IMPORTANT — PREMIUM:
- Return the TOTAL premium amount INCLUDING all taxes (GST, IGST, service tax).
- If only base premium and tax are shown separately, ADD them together.
- For multi-year policies paid as single payment, set premium to the TOTAL amount paid and premium_frequency to "single".

IMPORTANT — SUM INSURED:
- For health insurance, return the TOTAL sum insured including all benefits (base + secure benefit + plus benefit + restore benefit etc.)
- If the policy shows "Base Sum Insured" and additional benefits that increase the effective cover, sum them all up.

IMPORTANT — PLAN NAME:
- Use ONLY the official product/plan name (e.g., "Optima Secure Individual", "Care Freedom - Plan 2").
- Do NOT include email subject text, prefixes like "my:", "Re:", marketing language, or renewal notices in the plan name.

Return ONLY valid JSON. No markdown, no explanations."""

FINALIZE_PROMPT = """You are given a list of raw extracted insurance policies that may contain duplicates (same policy extracted from different emails/renewal documents).

Your job:
1. DEDUPLICATE: Group policies by policy_number (ignore trailing zeros and formatting differences). Keep the most complete version (fewest null fields). Prefer ACTIVE over EXPIRED when both exist for the same policy.
2. FIX STATUSES: Given today's date provided below, set status to "ACTIVE" if policy_end >= today, "EXPIRED" if policy_end < today. If no policy_end, set "UNKNOWN".
3. MERGE: If two entries for the same policy have complementary fields (one has nominee, the other has coverages), merge them into one complete record.
4. CLEAN: Remove any entries that are clearly not real policies (no policy_number AND no provider).

Return ONLY a JSON array of the final deduplicated policies. Same schema as input. No explanations."""


PASSWORD_HINTS = {
    "icicilombard": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "icici lombard": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "hdfc ergo": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "hdfcergo": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "bajaj allianz": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "bajajallianz": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "lic": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "sbi": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "tata aig": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "new india": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "star health": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "care health": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
    "oriental": "Your date of birth in DDMMYYYY format (e.g., 15061990)",
}


def _extract_policy_number_from_subject(subject: str) -> str | None:
    """Try to extract a policy number from an email subject line."""
    m = re.search(r'(?:policy\s*(?:no\.?|number)\s*:?\s*)([A-Z0-9/\-]+)', subject, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _guess_provider(email_from: str, subject: str) -> str:
    """Guess the insurance provider from email sender or subject."""
    combined = (email_from + " " + subject).lower()
    providers = [
        ("kotak", "Kotak Life"),
        ("hdfc ergo", "HDFC ERGO"),
        ("hdfc life", "HDFC Life"),
        ("icici pru", "ICICI Prudential"),
        ("icici lombard", "ICICI Lombard"),
        ("lic", "LIC"),
        ("max life", "Max Life"),
        ("sbi life", "SBI Life"),
        ("bajaj allianz", "Bajaj Allianz"),
        ("tata aia", "Tata AIA"),
        ("care health", "Care Health"),
        ("star health", "Star Health"),
        ("acko", "Acko"),
        ("royal sundaram", "Royal Sundaram"),
        ("niva bupa", "Niva Bupa"),
        ("digit", "Go Digit"),
    ]
    for key, name in providers:
        if key in combined:
            return name
    return "Unknown Provider"


def _get_password_hint(email_from: str, provider: str) -> str:
    """Return a password hint based on the insurer."""
    combined = ((email_from or "") + " " + (provider or "")).lower()
    for key, hint in PASSWORD_HINTS.items():
        if key in combined:
            return hint
    return "Usually your date of birth (DDMMYYYY) or PAN number"


def _strip_json(content: str) -> str:
    """Strip markdown code blocks from Grok response."""
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    if content.endswith("```"):
        content = content[:-3].strip()
    return content


class PipelineService:
    def __init__(self):
        # Use Groq if available (faster + cheaper), fall back to xAI Grok
        if os.getenv("GROQ_API_KEY"):
            self.client = AsyncOpenAI(
                api_key=os.getenv("GROQ_API_KEY"),
                base_url="https://api.groq.com/openai/v1",
            )
            self.model = "llama-3.1-8b-instant"
            logger.info("Using Groq (Llama 3.1 8B Instant) for extraction")
        else:
            self.client = AsyncOpenAI(
                api_key=os.getenv("XAI_API_KEY"),
                base_url="https://api.x.ai/v1",
            )
            self.model = "grok-4-1-fast-non-reasoning"
            logger.info("Using xAI Grok for extraction")
        self._triage = TriageService()

    # ── Stage 1: Triage (local ML) ───────────────────

    async def triage(
        self,
        email_metadata: list[dict],
        skip_msg_ids: set[str] | None = None,
        user_id: int | None = None,
    ):
        """Yield progress events. Final event has type='stage_complete' with relevant_emails."""
        t_stage = time.time()
        skip_msg_ids = skip_msg_ids or set()

        # Partition into cached vs new
        new_emails = [m for m in email_metadata if m["msg_id"] not in skip_msg_ids]
        cached_count = len(email_metadata) - len(new_emails)
        total = len(new_emails)

        if cached_count > 0:
            yield {
                "type": "progress",
                "stage": "triage",
                "pct": 5,
                "message": f"Reviewing {total} new emails...",
            }

        relevant = []
        skipped = 0

        if total > 0:
            yield {
                "type": "progress",
                "stage": "triage",
                "pct": 10,
                "message": f"Identifying insurance emails ({total} to review)...",
            }

            # Run Groq LLM triage (batched, ~2-3s for all emails)
            t0 = time.time()
            results = await self._triage.classify_batch_async(new_emails)
            logger.info(f"[Timing] Triage classify_batch: {time.time() - t0:.2f}s for {total} emails")

            t0 = time.time()
            triage_batch_stmts = []
            now = datetime.now().isoformat()
            for i, (is_relevant, reason, score) in enumerate(results):
                meta = new_emails[i]
                if is_relevant:
                    relevant.append(meta)
                    logger.info(f"[Triage YES] {meta['subject'][:60]} — {reason}")
                else:
                    skipped += 1
                    logger.info(f"[Triage NO]  {meta['subject'][:60]} — {reason}")

                # Collect for batch DB save
                if user_id is not None:
                    triage_batch_stmts.append((
                        """INSERT OR REPLACE INTO processed_emails
                           (msg_id, user_id, is_relevant, triage_reason, processed_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        [meta["msg_id"], user_id, int(is_relevant), reason, now],
                    ))

            # Batch save all triage results in one round-trip
            if triage_batch_stmts:
                try:
                    from services.db_service import db as turso_db
                    # libsql_client.batch() takes list of InStatement (sql, args) tuples
                    await turso_db._client.batch(triage_batch_stmts)
                except Exception as e:
                    logger.warning(f"Batch triage save failed, falling back to individual: {e}")
                    # Fallback: save individually
                    from services.db_service import db as turso_db
                    for stmt, args in triage_batch_stmts:
                        try:
                            await turso_db.execute(stmt, args)
                        except Exception:
                            pass
            logger.info(f"[Timing] Triage DB saves: {time.time() - t0:.2f}s for {total} emails")

            yield {
                "type": "progress",
                "stage": "triage",
                "pct": 35,
                "message": f"Found {len(relevant)} insurance emails out of {total}",
            }

        elapsed = time.time() - t_stage
        logger.info(f"[Timing] TRIAGE TOTAL: {elapsed:.2f}s — {len(relevant)} relevant, {skipped} skipped, {cached_count} cached")

        yield {
            "type": "stage_complete",
            "stage": "triage",
            "relevant": len(relevant),
            "skipped": skipped,
            "cached": cached_count,
            "relevant_emails": relevant,
            "message": f"Found {len(relevant)} insurance emails in {elapsed:.1f}s",
        }

    # ── Stage 2: Extract ─────────────────────────────

    async def extract(
        self,
        gmail_service,
        relevant_emails: list[dict],
        skip_msg_ids: set[str] | None = None,
        user_id: int | None = None,
        vault_key_derived: bytes | None = None,
    ):
        """Download PDFs sequentially (Gmail API not thread-safe), then send to LLM concurrently.
        Yields progress events. Final event has type='stage_complete' with raw_policies.
        """
        t_stage = time.time()
        skip_msg_ids = skip_msg_ids or set()
        new_emails = [m for m in relevant_emails if m["msg_id"] not in skip_msg_ids]
        cached_count = len(relevant_emails) - len(new_emails)
        total = len(new_emails)

        if cached_count > 0:
            yield {
                "type": "progress",
                "stage": "extract",
                "pct": 35,
                "message": f"Processing {total} new emails...",
            }

        # Deduplicate emails by msg_id (same email can appear in multiple triage passes)
        seen_ids = set()
        deduped = []
        for m in new_emails:
            if m["msg_id"] not in seen_ids:
                seen_ids.add(m["msg_id"])
                deduped.append(m)
        if len(deduped) < len(new_emails):
            logger.info(f"[Extract] Deduped {len(new_emails)} → {len(deduped)} emails")
            new_emails = deduped
            total = len(new_emails)

        # Process emails in batches: download PDF, extract via LLM, discard PDF text
        # This keeps memory bounded instead of loading all PDFs at once
        BATCH_SIZE = 5
        raw_policies = []
        llm_completed = 0
        total_download_time = 0.0
        total_llm_time = 0.0

        for batch_start in range(0, total, BATCH_SIZE):
            batch_emails = new_emails[batch_start:batch_start + BATCH_SIZE]
            batch_docs = []

            # Download PDFs for this batch (with 60s timeout per email)
            DOWNLOAD_TIMEOUT = 60
            for i, meta in enumerate(batch_emails):
                global_idx = batch_start + i
                yield {
                    "type": "progress",
                    "stage": "extract",
                    "current": global_idx,
                    "total": total,
                    "pct": int(35 + (global_idx / max(total, 1)) * 20),
                    "message": f"Reading email {global_idx + 1} of {total}...",
                }
                t0 = time.time()
                try:
                    docs = await asyncio.wait_for(
                        asyncio.to_thread(gmail_service.fetch_document_text, meta["msg_id"]),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    dl_elapsed = time.time() - t0
                    total_download_time += dl_elapsed
                    logger.warning(f"[Timing] Download {global_idx + 1}/{total}: TIMEOUT after {dl_elapsed:.1f}s — {meta['subject'][:50]}")
                    continue
                dl_elapsed = time.time() - t0
                total_download_time += dl_elapsed
                logger.info(f"[Timing] Download {global_idx + 1}/{total}: {dl_elapsed:.2f}s — {meta['subject'][:50]}")
                for doc in docs:
                    doc["_msg_id"] = meta["msg_id"]
                batch_docs.extend(docs)

            logger.info(f"[Timing] Batch download done: {len(batch_docs)} docs from {len(batch_emails)} emails")

            # Extract this batch via LLM concurrently
            if batch_docs:
                sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)

                async def llm_one(doc):
                    nonlocal llm_completed, total_llm_time
                    async with sem:
                        t0 = time.time()
                        result = await self._grok_extract(doc)
                        llm_elapsed = time.time() - t0
                        total_llm_time += llm_elapsed
                        llm_completed += 1
                        status = "extracted" if result else "skipped"
                        logger.info(f"[Timing] LLM {llm_completed}/{total}: {llm_elapsed:.2f}s — {status} — {doc['pdf_filename'][:50]}")
                        if result:
                            raw_policies.append(result)
                            if user_id is not None and vault_key_derived is not None:
                                msg_id = doc.get("_msg_id")
                                if msg_id:
                                    try:
                                        await db_service.save_extraction_result(
                                            msg_id, user_id, result, vault_key_derived
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to save extraction for {msg_id}: {e}")
                    return {
                        "type": "progress",
                        "stage": "extract",
                        "pct": int(55 + (llm_completed / max(total, 1)) * 30),
                        "message": f"Extracting policy details ({llm_completed} of {total})...",
                    }

                tasks = [asyncio.create_task(llm_one(d)) for d in batch_docs]
                for coro in asyncio.as_completed(tasks):
                    event = await coro
                    yield event
            # batch_docs goes out of scope, freeing PDF text memory

        elapsed = time.time() - t_stage
        logger.info(
            f"[Timing] EXTRACT TOTAL: {elapsed:.2f}s — "
            f"downloads: {total_download_time:.2f}s, LLM calls: {total_llm_time:.2f}s, "
            f"{len(raw_policies)} extracted, {cached_count} cached"
        )

        if not raw_policies and cached_count == 0:
            yield {
                "type": "stage_complete",
                "stage": "extract",
                "count": 0,
                "cached": 0,
                "raw_policies": [],
                "message": "No policy documents found",
            }
            return

        yield {
            "type": "stage_complete",
            "stage": "extract",
            "count": len(raw_policies),
            "cached": cached_count,
            "raw_policies": raw_policies,
            "message": f"Found {len(raw_policies)} policies in {elapsed:.1f}s",
        }

    # ── Stage 2 (Modal): Extract via Modal workers ───────

    async def extract_modal(
        self,
        token_json: str,
        user_email: str,
        relevant_emails: list[dict],
        skip_msg_ids: set[str] | None = None,
        user_id: int | None = None,
        vault_key_derived: bytes | None = None,
    ):
        """Modal-powered extraction: fans out PDF downloads + LLM calls in parallel.
        Yields progress events. Final event has type='stage_complete' with raw_policies.
        """
        t_stage = time.time()
        skip_msg_ids = skip_msg_ids or set()
        new_emails = [m for m in relevant_emails if m["msg_id"] not in skip_msg_ids]
        cached_count = len(relevant_emails) - len(new_emails)
        total = len(new_emails)

        # Deduplicate by msg_id
        seen_ids = set()
        deduped = []
        for m in new_emails:
            if m["msg_id"] not in seen_ids:
                seen_ids.add(m["msg_id"])
                deduped.append(m)
        new_emails = deduped
        total = len(new_emails)

        if total == 0:
            yield {
                "type": "stage_complete",
                "stage": "extract",
                "count": 0,
                "cached": cached_count,
                "raw_policies": [],
                "message": "No new emails to process",
            }
            return

        yield {
            "type": "progress",
            "stage": "extract",
            "pct": 38,
            "message": f"Processing {total} emails via Modal workers...",
        }

        # Build LLM config
        if os.getenv("GROQ_API_KEY"):
            llm_config = {
                "api_key": os.getenv("GROQ_API_KEY"),
                "base_url": "https://api.groq.com/openai/v1",
                "model": "llama-3.1-8b-instant",
            }
        else:
            llm_config = {
                "api_key": os.getenv("XAI_API_KEY"),
                "base_url": "https://api.x.ai/v1",
                "model": "grok-4-1-fast-non-reasoning",
            }

        msg_ids = [m["msg_id"] for m in new_emails]

        yield {
            "type": "progress",
            "stage": "extract",
            "pct": 42,
            "message": f"Downloading PDFs + extracting in parallel ({total} emails)...",
        }

        # Call deployed Modal function (lookup by name, no app context needed)
        import modal
        process_emails_fn = modal.Function.from_name("insurance-track", "process_emails")

        raw_policies = await asyncio.to_thread(
            process_emails_fn.remote,
            token_json,
            user_email,
            msg_ids,
            llm_config,
        )

        elapsed = time.time() - t_stage
        logger.info(
            f"[Timing] EXTRACT_MODAL TOTAL: {elapsed:.2f}s — "
            f"{len(raw_policies)} extracted, {cached_count} cached"
        )

        # Save extractions to DB (locally, after Modal returns)
        # Group by msg_id since one email can produce multiple policies
        if raw_policies and user_id is not None and vault_key_derived is not None:
            from collections import defaultdict
            by_msg = defaultdict(list)
            for policy in raw_policies:
                msg_id = policy.get("source_msg_id", "")
                if msg_id:
                    by_msg[msg_id].append(policy)
            for msg_id, policies in by_msg.items():
                try:
                    data = policies[0] if len(policies) == 1 else policies
                    await db_service.save_extraction_result(
                        msg_id, user_id, data, vault_key_derived
                    )
                except Exception as e:
                    logger.warning(f"Failed to save extraction for {msg_id}: {e}")

        if not raw_policies and cached_count == 0:
            yield {
                "type": "stage_complete",
                "stage": "extract",
                "count": 0,
                "cached": 0,
                "raw_policies": [],
                "message": "No policy documents found",
            }
            return

        yield {
            "type": "stage_complete",
            "stage": "extract",
            "count": len(raw_policies),
            "cached": cached_count,
            "raw_policies": raw_policies,
            "message": f"Found {len(raw_policies)} policies in {elapsed:.1f}s (Modal)",
        }

    async def _grok_extract(self, doc: dict) -> dict | None:
        """Send a single document's text to the LLM for extraction."""
        is_locked = doc.get("_password_protected", False)
        truncated = doc["pdf_text"][:15000]
        user_msg = (
            f"Filename: {doc['pdf_filename']}\n"
            f"Email subject: {doc['email_subject']}\n\n"
            f"Document text:\n{truncated}"
        )
        try:
            t0 = time.time()
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=2000,
            )
            api_elapsed = time.time() - t0
            usage = response.usage
            tokens_info = f"in={usage.prompt_tokens},out={usage.completion_tokens}" if usage else "no-usage"
            logger.info(f"[Timing] LLM API call: {api_elapsed:.2f}s ({tokens_info}) — {doc['pdf_filename'][:50]}")

            content = _strip_json(response.choices[0].message.content.strip())
            result = json.loads(content)
            if result and not result.get("skip"):
                result["source_pdf"] = doc["pdf_filename"]
                result["source_email"] = doc["email_subject"]
                result["source_msg_id"] = doc.get("_msg_id", "")
                if is_locked:
                    result["password_protected"] = True
                    result["locked_pdf_path"] = doc.get("_locked_pdf_path", "")
                    email_hint = doc.get("_password_hint", "")
                    result["password_hint"] = email_hint or _get_password_hint(
                        doc.get("email_from", ""), result.get("provider", "")
                    )
                return result
            else:
                logger.info(f"[Timing] LLM returned skip for {doc['pdf_filename'][:50]}")
                # If PDF was locked, still surface it as a locked card
                if is_locked:
                    pn = _extract_policy_number_from_subject(doc.get("email_subject", ""))
                    email_hint = doc.get("_password_hint", "")
                    return {
                        "provider": _guess_provider(doc.get("email_from", ""), doc.get("email_subject", "")),
                        "policy_number": pn,
                        "password_protected": True,
                        "locked_pdf_path": doc.get("_locked_pdf_path", ""),
                        "password_hint": email_hint or _get_password_hint(
                            doc.get("email_from", ""), ""
                        ),
                        "source_pdf": doc["pdf_filename"],
                        "source_email": doc["email_subject"],
                        "source_msg_id": doc.get("_msg_id", ""),
                    }
                return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {doc['pdf_filename']}: {e}")
        except Exception as e:
            logger.error(f"LLM API error for {doc['pdf_filename']}: {e}")
        return None

    # ── Stage 3: Finalize ────────────────────────────

    async def finalize(
        self,
        raw_policies: list[dict],
        existing_policies: list[dict] | None = None,
    ) -> list[dict]:
        """Deduplicate and fix statuses using deterministic local logic.
        Merges new raw_policies with existing cached extractions before dedup."""
        t0 = time.time()
        combined = list(existing_policies or []) + list(raw_policies)
        if not combined:
            return []
        result = self._local_dedup(combined)
        logger.info(f"[Timing] FINALIZE: {time.time() - t0:.2f}s — {len(combined)} input → {len(result)} output")
        return result

    def _local_dedup(self, policies: list[dict]) -> list[dict]:
        """Deduplicate policies using normalized policy numbers."""
        today = datetime.now().date()

        for p in policies:
            end = p.get("policy_end")
            if end:
                try:
                    end_date = datetime.strptime(end, "%Y-%m-%d").date()
                    p["status"] = "ACTIVE" if end_date >= today else "EXPIRED"
                except (ValueError, TypeError):
                    pass

        def normalize_pn(pn):
            if not pn:
                return ""
            cleaned = re.sub(r'[\s\-]', '', pn)
            # Acko-style: strip /NN suffix (e.g. /02, /03)
            cleaned = re.sub(r'/\d{2}$', '', cleaned)
            # HDFC-style: long numeric policy numbers have renewal suffixes.
            # Truncate to first 15 digits to group renewal chains
            # (e.g. 2856205745802501 and 2856205745802502000 both → 285620574580250)
            if cleaned.isdigit() and len(cleaned) > 15:
                cleaned = cleaned[:15]
            return cleaned

        def _merge(winner, loser):
            """Fill null fields in winner from loser, and prefer better values for key fields."""
            skip_keys = ('status', 'source_pdf', 'source_email', 'source_msg_id',
                         'password_protected', 'locked_pdf_path', 'password_hint')
            for key in loser:
                if key in skip_keys:
                    continue
                if winner.get(key) is None and loser.get(key) is not None:
                    winner[key] = loser[key]
            # Always prefer the LATER policy_end (even if winner already has one)
            w_end = winner.get("policy_end") or ""
            l_end = loser.get("policy_end") or ""
            if l_end > w_end:
                winner["policy_end"] = loser["policy_end"]
                # Also take the matching start date if available
                if loser.get("policy_start"):
                    winner["policy_start"] = loser["policy_start"]
            # Prefer higher sum_insured (covers total with benefits vs base only)
            w_si = winner.get("sum_insured") or 0
            l_si = loser.get("sum_insured") or 0
            if isinstance(l_si, (int, float)) and isinstance(w_si, (int, float)) and l_si > w_si:
                winner["sum_insured"] = loser["sum_insured"]
            # Prefer higher premium (total with tax vs base without)
            w_pr = winner.get("premium") or 0
            l_pr = loser.get("premium") or 0
            if isinstance(l_pr, (int, float)) and isinstance(w_pr, (int, float)) and l_pr > w_pr:
                winner["premium"] = loser["premium"]
            # If either side is non-locked, drop the locked flag from winner
            if not loser.get("password_protected") or not winner.get("password_protected"):
                winner.pop("password_protected", None)
                winner.pop("locked_pdf_path", None)
                winner.pop("password_hint", None)
            # Prefer more detailed insured_members (more non-null fields)
            w_members = winner.get("insured_members") or []
            l_members = loser.get("insured_members") or []
            if w_members and l_members:
                w_detail = sum(1 for m in w_members for v in m.values() if v is not None)
                l_detail = sum(1 for m in l_members for v in m.values() if v is not None)
                if l_detail > w_detail:
                    winner["insured_members"] = l_members
            return winner

        seen = {}
        for p in policies:
            pn = normalize_pn(p.get("policy_number", ""))
            key = pn if pn else f"_no_pn_{id(p)}"

            # Log raw extraction for debugging
            logger.info(
                f"[Dedup] Input: pn={p.get('policy_number')} key={key} "
                f"start={p.get('policy_start')} end={p.get('policy_end')} "
                f"status={p.get('status')} src={p.get('source_email', '')[:50]}"
            )

            if key in seen:
                existing = seen[key]
                existing_active = existing.get("status") == "ACTIVE"
                new_active = p.get("status") == "ACTIVE"
                logger.info(
                    f"[Dedup] Merging key={key}: existing_end={existing.get('policy_end')} "
                    f"new_end={p.get('policy_end')} existing_active={existing_active} new_active={new_active}"
                )
                if new_active and not existing_active:
                    seen[key] = _merge(p, existing)
                elif not new_active and existing_active:
                    seen[key] = _merge(existing, p)
                else:
                    # Same status — prefer later end date, then fewer nulls
                    ex_end = existing.get("policy_end") or ""
                    new_end = p.get("policy_end") or ""
                    if new_end > ex_end:
                        seen[key] = _merge(p, existing)
                    elif new_end < ex_end:
                        seen[key] = _merge(existing, p)
                    elif sum(1 for v in p.values() if v is None) < sum(1 for v in existing.values() if v is None):
                        seen[key] = _merge(p, existing)
                    else:
                        seen[key] = _merge(existing, p)
                merged = seen[key]
                logger.info(
                    f"[Dedup] Result key={key}: end={merged.get('policy_end')} "
                    f"status={merged.get('status')} src={merged.get('source_email', '')[:50]}"
                )
            else:
                seen[key] = p

        # Final pass: re-fix statuses after merge may have added end dates
        result = list(seen.values())
        for p in result:
            end = p.get("policy_end")
            if end:
                try:
                    end_date = datetime.strptime(end, "%Y-%m-%d").date()
                    p["status"] = "ACTIVE" if end_date >= today else "EXPIRED"
                except (ValueError, TypeError):
                    pass
            # Enrich LIC plan numbers with human-readable names
            plan = p.get("plan_name") or ""
            provider = (p.get("provider") or "").lower()
            if plan.strip() in LIC_PLAN_NAMES and "lic" in provider:
                p["plan_name"] = LIC_PLAN_NAMES[plan.strip()]
            # Clean garbled plan names (strip email subject artifacts)
            if plan:
                # Remove common prefixes from email subjects
                cleaned = re.sub(r'^(my:\s*|re:\s*|fwd:\s*|fw:\s*)', '', plan, flags=re.IGNORECASE).strip()
                # Remove "Welcome Renew :" etc.
                cleaned = re.sub(r'^(welcome\s+renew\s*:?\s*)', '', cleaned, flags=re.IGNORECASE).strip()
                if cleaned and cleaned != plan:
                    p["plan_name"] = cleaned
        return result
