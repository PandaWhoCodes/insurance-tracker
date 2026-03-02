import asyncio
import json
import logging
import os
import re
from datetime import datetime

from openai import AsyncOpenAI

from services import db_service
from services.triage_service import TriageService

logger = logging.getLogger(__name__)

EXTRACT_CONCURRENCY = 3

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
    "type": "health" | "car" | "term_life",
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
    "premium_frequency": "yearly | monthly | one_time | quarterly",
    "policy_start": "YYYY-MM-DD or null",
    "policy_end": "YYYY-MM-DD or null",
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
        self.client = AsyncOpenAI(
            api_key=os.getenv("XAI_API_KEY"),
            base_url="https://api.x.ai/v1",
        )
        self.model = "grok-4-1-fast-reasoning"
        self._triage = TriageService()

    # ── Stage 1: Triage (local ML) ───────────────────

    async def triage(
        self,
        email_metadata: list[dict],
        skip_msg_ids: set[str] | None = None,
        user_id: int | None = None,
    ):
        """Yield progress events. Final event has type='stage_complete' with relevant_emails."""
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
                "message": f"{cached_count} cached, triaging {total} new...",
            }

        relevant = []
        skipped = 0

        if total > 0:
            yield {
                "type": "progress",
                "stage": "triage",
                "pct": 10,
                "message": f"Classifying {total} emails locally...",
            }

            # Run local ML triage (batch, ~0.3s for all emails)
            results = await asyncio.to_thread(
                self._triage.classify_batch, new_emails
            )

            for i, (is_relevant, reason, score) in enumerate(results):
                meta = new_emails[i]
                if is_relevant:
                    relevant.append(meta)
                    logger.info(f"[Triage YES] {meta['subject'][:60]} — {reason}")
                else:
                    skipped += 1
                    logger.info(f"[Triage NO]  {meta['subject'][:60]} — {reason}")

                # Save to DB
                if user_id is not None:
                    try:
                        await db_service.save_triage_result(
                            meta["msg_id"], user_id, is_relevant, reason
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save triage for {meta['msg_id']}: {e}")

            yield {
                "type": "progress",
                "stage": "triage",
                "pct": 35,
                "message": f"Classified {total} emails: {len(relevant)} relevant",
            }

        yield {
            "type": "stage_complete",
            "stage": "triage",
            "relevant": len(relevant),
            "skipped": skipped,
            "cached": cached_count,
            "relevant_emails": relevant,
            "message": f"Triage done: {len(relevant)} relevant, {skipped} skipped, {cached_count} cached",
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
        """Download PDFs sequentially (Gmail API not thread-safe), then send to Grok concurrently.
        Yields progress events. Final event has type='stage_complete' with raw_policies.
        """
        skip_msg_ids = skip_msg_ids or set()
        new_emails = [m for m in relevant_emails if m["msg_id"] not in skip_msg_ids]
        cached_count = len(relevant_emails) - len(new_emails)
        total = len(new_emails)

        if cached_count > 0:
            yield {
                "type": "progress",
                "stage": "extract",
                "pct": 35,
                "message": f"{cached_count} cached extractions, processing {total} new...",
            }

        # Step 2a: Download all PDFs sequentially (Gmail API shares one SSL connection)
        all_docs = []
        for i, meta in enumerate(new_emails):
            yield {
                "type": "progress",
                "stage": "extract",
                "current": i,
                "total": total,
                "pct": int(35 + (i / max(total, 1)) * 20),
                "message": f"Downloading {i + 1}/{total}: {meta['subject'][:40]}...",
            }
            docs = await asyncio.to_thread(gmail_service.fetch_document_text, meta["msg_id"])
            # Tag each doc with its parent msg_id
            for doc in docs:
                doc["_msg_id"] = meta["msg_id"]
            all_docs.extend(docs)

        if not all_docs and cached_count == 0:
            yield {
                "type": "stage_complete",
                "stage": "extract",
                "count": 0,
                "cached": 0,
                "raw_policies": [],
                "message": "No documents to extract",
            }
            return

        # Step 2b: Send to Grok concurrently (async HTTP is fine)
        sem = asyncio.Semaphore(EXTRACT_CONCURRENCY)
        grok_total = len(all_docs)
        completed = 0
        raw_policies = []

        async def grok_one(doc):
            nonlocal completed
            async with sem:
                result = await self._grok_extract(doc)
                completed += 1
                if result:
                    raw_policies.append(result)
                    # Save extraction to DB (encrypted)
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
                    "current": completed,
                    "total": grok_total,
                    "pct": int(55 + (completed / max(grok_total, 1)) * 30),
                    "message": f"Analyzing {completed}/{grok_total}: {doc['pdf_filename'][:40]}...",
                }

        if grok_total > 0:
            tasks = [asyncio.create_task(grok_one(d)) for d in all_docs]
            for coro in asyncio.as_completed(tasks):
                event = await coro
                yield event

        yield {
            "type": "stage_complete",
            "stage": "extract",
            "count": len(raw_policies),
            "cached": cached_count,
            "raw_policies": raw_policies,
            "message": f"Extracted {len(raw_policies)} new, {cached_count} cached",
        }

    async def _grok_extract(self, doc: dict) -> dict | None:
        """Send a single document's text to Grok for extraction."""
        is_locked = doc.get("_password_protected", False)
        truncated = doc["pdf_text"][:15000]
        user_msg = (
            f"Filename: {doc['pdf_filename']}\n"
            f"Email subject: {doc['email_subject']}\n\n"
            f"Document text:\n{truncated}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                max_tokens=2000,
            )
            content = _strip_json(response.choices[0].message.content.strip())
            result = json.loads(content)
            if result and not result.get("skip"):
                result["source_pdf"] = doc["pdf_filename"]
                result["source_email"] = doc["email_subject"]
                if is_locked:
                    result["password_protected"] = True
                    result["locked_pdf_path"] = doc.get("_locked_pdf_path", "")
                    # Prefer hint extracted from email body, fall back to generic
                    email_hint = doc.get("_password_hint", "")
                    result["password_hint"] = email_hint or _get_password_hint(
                        doc.get("email_from", ""), result.get("provider", "")
                    )
                return result
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {doc['pdf_filename']}: {e}")
        except Exception as e:
            logger.error(f"Grok API error for {doc['pdf_filename']}: {e}")
        return None

    # ── Stage 3: Finalize ────────────────────────────

    async def finalize(
        self,
        raw_policies: list[dict],
        existing_policies: list[dict] | None = None,
    ) -> list[dict]:
        """Deduplicate and fix statuses using deterministic local logic.
        Merges new raw_policies with existing cached extractions before dedup."""
        combined = list(existing_policies or []) + list(raw_policies)
        if not combined:
            return []
        return self._local_dedup(combined)

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
            """Fill null fields in winner from loser."""
            for key in loser:
                if key in ('status', 'source_pdf', 'source_email'):
                    continue
                if winner.get(key) is None and loser.get(key) is not None:
                    winner[key] = loser[key]
            # If we have a non-locked version, drop the locked flag
            if loser.get("password_protected") is None and winner.get("password_protected"):
                del winner["password_protected"]
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

            if key in seen:
                existing = seen[key]
                existing_active = existing.get("status") == "ACTIVE"
                new_active = p.get("status") == "ACTIVE"
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
        return result
