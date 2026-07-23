import json
import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert insurance document analyzer. Given extracted text from an insurance policy PDF or email, extract structured policy information.

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


class GrokService:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("XAI_API_KEY"),
            base_url="https://api.x.ai/v1",
        )
        self.model = "grok-4-1-fast-reasoning"

    def extract_policies(self, pdf_texts: list[dict]) -> list[dict]:
        """Extract policy data from multiple PDFs.

        Args:
            pdf_texts: list of {"email_subject", "email_from", "email_date", "pdf_filename", "pdf_text"}
        Returns:
            list of extracted policy dicts
        """
        policies = []
        for i, pdf in enumerate(pdf_texts, 1):
            logger.info(f"[Grok {i}/{len(pdf_texts)}] Extracting from: {pdf['pdf_filename']}")
            result = self._extract_single(
                pdf["pdf_text"], pdf["pdf_filename"], pdf["email_subject"]
            )
            if result and not result.get("skip"):
                result["source_pdf"] = pdf["pdf_filename"]
                result["source_email"] = pdf["email_subject"]
                policies.append(result)

        return self._deduplicate(policies)

    def _extract_single(self, text: str, filename: str, email_subject: str) -> dict | None:
        truncated = text[:15000]

        user_message = f"""Extract policy information from this insurance document.

Filename: {filename}
Email subject: {email_subject}

Document text:
{truncated}"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0,
                max_tokens=2000,
            )

            content = response.choices[0].message.content.strip()

            # Strip markdown code blocks
            if content.startswith("```"):
                content = content.split("```")[1]
                content = content.removeprefix("json")
                content = content.strip()
            if content.endswith("```"):
                content = content[:-3].strip()

            return json.loads(content)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Grok response for {filename}: {e}")
            return None
        except Exception as e:
            logger.error(f"Grok API error for {filename}: {e}")
            return None

    def _fix_statuses(self, policies: list[dict]) -> list[dict]:
        """Override Grok's status using actual dates."""
        from datetime import datetime
        today = datetime.now().date()
        for p in policies:
            end = p.get("policy_end")
            if end:
                try:
                    end_date = datetime.strptime(end, "%Y-%m-%d").date()
                    p["status"] = "ACTIVE" if end_date >= today else "EXPIRED"
                except (ValueError, TypeError):
                    pass
        return policies

    def _deduplicate(self, policies: list[dict]) -> list[dict]:
        import re

        policies = self._fix_statuses(policies)

        def normalize_pn(pn: str) -> str:
            """Strip trailing zeros, spaces, slashes to group same policy."""
            if not pn:
                return ""
            # Remove spaces, dashes
            cleaned = re.sub(r'[\s\-]', '', pn)
            # Strip trailing 000 (HDFC ERGO appends these)
            cleaned = re.sub(r'0{3}$', '', cleaned)
            return cleaned

        def dedup_key(p: dict) -> str:
            """Create a key for grouping: normalized policy number + plan name."""
            pn = normalize_pn(p.get("policy_number", ""))
            plan = (p.get("plan_name") or "").lower().strip()
            # Remove prefixes like "my:" that Grok sometimes extracts
            plan = re.sub(r'^my:\s*', '', plan)
            return f"{pn}|{plan}" if pn else ""

        seen = {}
        for p in policies:
            key = dedup_key(p)
            if not key:
                seen[f"_no_pn_{id(p)}"] = p
                continue

            if key in seen:
                existing = seen[key]
                # Prefer ACTIVE over EXPIRED, then prefer more fields
                existing_active = existing.get("status") == "ACTIVE"
                new_active = p.get("status") == "ACTIVE"
                if new_active and not existing_active:
                    seen[key] = p
                elif existing_active == new_active:
                    existing_nulls = sum(1 for v in existing.values() if v is None)
                    new_nulls = sum(1 for v in p.values() if v is None)
                    if new_nulls < existing_nulls:
                        seen[key] = p
            else:
                seen[key] = p

        return list(seen.values())
