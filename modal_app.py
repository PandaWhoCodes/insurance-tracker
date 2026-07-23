"""Modal workers for insurance pipeline — parallel PDF download/extract + LLM extraction."""

import json
import logging
import time

import modal

logger = logging.getLogger(__name__)

app = modal.App("insurance-track")

# Modal image with all dependencies for Gmail + PDF + LLM
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "google-api-python-client",
    "google-auth",
    "google-auth-oauthlib",
    "PyMuPDF",
    "openai",
)

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

IMPORTANT — DATES:
- If the document contains multiple policy periods (e.g., original and renewal), always use the LATEST policy period dates.
- For multi-year policies (e.g., 2-year or 3-year), set policy_end to the FINAL expiry date, not an intermediate year.
- Look carefully for "Policy Period", "Period of Insurance", "Risk Start Date / End Date" fields.

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


def _strip_json(content: str) -> str:
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    if content.endswith("```"):
        content = content[:-3].strip()
    return content


def _get_password_hint(email_from: str, provider: str) -> str:
    combined = ((email_from or "") + " " + (provider or "")).lower()
    for key, hint in PASSWORD_HINTS.items():
        if key in combined:
            return hint
    return "Usually your date of birth (DDMMYYYY) or PAN number"


def _guess_provider(email_from: str, subject: str) -> str:
    combined = (email_from + " " + subject).lower()
    providers = [
        ("kotak", "Kotak Life"), ("hdfc ergo", "HDFC ERGO"),
        ("hdfc life", "HDFC Life"), ("icici pru", "ICICI Prudential"),
        ("icici lombard", "ICICI Lombard"), ("lic", "LIC"),
        ("max life", "Max Life"), ("sbi life", "SBI Life"),
        ("bajaj allianz", "Bajaj Allianz"), ("tata aia", "Tata AIA"),
        ("care health", "Care Health"), ("star health", "Star Health"),
        ("acko", "Acko"), ("royal sundaram", "Royal Sundaram"),
        ("niva bupa", "Niva Bupa"), ("digit", "Go Digit"),
    ]
    for key, name in providers:
        if key in combined:
            return name
    return "Unknown Provider"



def _extract_policy_number_from_subject(subject: str) -> str | None:
    m = re.search(r'(?:policy\s*(?:no\.?|number)\s*:?\s*)([A-Z0-9/\-]+)', subject, re.IGNORECASE)
    return m.group(1).strip() if m else None


# Size threshold: emails with attachments > 10MB get the large container
LARGE_PDF_THRESHOLD_BYTES = 10 * 1024 * 1024  # 10 MB


# ── Core logic (shared by both container sizes) ──────────


def _do_fetch_and_extract(token_json: str, msg_id: str, user_email: str) -> list[dict]:
    """Core logic for downloading PDFs and extracting text from one email."""
    import base64
    import tempfile

    import fitz
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    t_total = time.time()

    # Reconstruct Gmail service from token
    creds = Credentials.from_authorized_user_info(
        json.loads(token_json),
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds)

    # Get full message detail
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("Subject", "(no subject)")
    email_from = headers.get("From", "")
    email_date = headers.get("Date", "")

    results = []

    # Download + extract PDFs
    def process_parts(parts_list):
        for part in parts_list:
            if part.get("parts"):
                process_parts(part["parts"])

            filename = part.get("filename", "")
            mime_type = part.get("mimeType", "")
            if not filename:
                continue
            if not (
                mime_type.startswith("application/pdf")
                or mime_type.startswith("application/octet-stream")
                or filename.lower().endswith(".pdf")
            ):
                continue

            attachment_id = part.get("body", {}).get("attachmentId")
            if not attachment_id:
                continue

            try:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=attachment_id
                ).execute()
                data = base64.urlsafe_b64decode(att["data"])

                # Write to temp file for PyMuPDF
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = tmp.name

                # Extract text
                doc = fitz.open(tmp_path)
                is_locked = doc.is_encrypted
                if is_locked:
                    doc.close()
                    hint = _find_password_hint(msg["payload"])
                    results.append({
                        "email_subject": subject,
                        "email_from": email_from,
                        "email_date": email_date,
                        "pdf_filename": filename,
                        "pdf_text": f"[PASSWORD-PROTECTED PDF]\nFilename: {filename}\nEmail subject: {subject}",
                        "_password_protected": True,
                        "_locked_pdf_path": "",
                        "_password_hint": hint,
                        "_msg_id": msg_id,
                    })
                    continue

                text = ""
                for page in doc[:10]:  # first 10 pages — all policy info is there
                    page_text = page.get_text()
                    if page_text:
                        text += page_text + "\n"
                doc.close()

                if text and len(text.strip()) > 100:
                    results.append({
                        "email_subject": subject,
                        "email_from": email_from,
                        "email_date": email_date,
                        "pdf_filename": filename,
                        "pdf_text": text,
                        "_msg_id": msg_id,
                    })
            except Exception as e:
                err_repr = repr(e).lower()
                if "password" in err_repr or "encrypted" in err_repr:
                    results.append({
                        "email_subject": subject,
                        "email_from": email_from,
                        "email_date": email_date,
                        "pdf_filename": filename,
                        "pdf_text": f"[PASSWORD-PROTECTED PDF]\nFilename: {filename}\nEmail subject: {subject}",
                        "_password_protected": True,
                        "_locked_pdf_path": "",
                        "_password_hint": "",
                        "_msg_id": msg_id,
                    })
                else:
                    print(f"Error downloading {filename}: {e}")

    parts = msg["payload"].get("parts", [])
    if not parts and msg["payload"].get("body"):
        parts = [msg["payload"]]
    process_parts(parts)

    # Also check email body for policy info
    body_text = _extract_body_text_from_payload(msg["payload"])
    if body_text and len(body_text.strip()) > 200:
        body_lower = body_text.lower()
        if any(kw in body_lower for kw in [
            "policy no", "policy number", "sum insured", "premium",
            "policy period", "insured value"
        ]):
            results.append({
                "email_subject": subject,
                "email_from": email_from,
                "email_date": email_date,
                "pdf_filename": f"email_body_{msg_id}",
                "pdf_text": body_text[:10000],
                "_msg_id": msg_id,
            })

    elapsed = time.time() - t_total
    print(f"[Modal] fetch_and_extract: {elapsed:.2f}s — {len(results)} docs from {subject[:50]}")
    return results


# ── Modal Functions: two tiers by size ───────────────────


@app.function(image=image, timeout=120, cpu=0.5, memory=512)
def fetch_and_extract_pdf(token_json: str, msg_id: str, user_email: str) -> list[dict]:
    """Standard tier: emails with attachments ≤ 10MB."""
    return _do_fetch_and_extract(token_json, msg_id, user_email)


@app.function(image=image, timeout=180, cpu=1.0, memory=2048)
def fetch_and_extract_pdf_large(token_json: str, msg_id: str, user_email: str) -> list[dict]:
    """Large tier: emails with attachments > 10MB. 2GB memory for big PDFs."""
    return _do_fetch_and_extract(token_json, msg_id, user_email)


@app.function(image=image, timeout=60, cpu=0.25, memory=256)
def extract_policy(doc: dict, llm_config: dict) -> dict | None:
    """Call LLM to extract policy from document text. Runs on Modal for parallelism."""
    from openai import OpenAI

    t0 = time.time()
    is_locked = doc.get("_password_protected", False)
    truncated = doc["pdf_text"][:15000]
    user_msg = (
        f"Filename: {doc['pdf_filename']}\n"
        f"Email subject: {doc['email_subject']}\n\n"
        f"Document text:\n{truncated}"
    )

    try:
        client = OpenAI(
            api_key=llm_config["api_key"],
            base_url=llm_config["base_url"],
        )
        response = client.chat.completions.create(
            model=llm_config["model"],
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=2000,
        )

        content = _strip_json(response.choices[0].message.content.strip())
        result = json.loads(content)

        elapsed = time.time() - t0
        usage = response.usage
        tokens_info = f"in={usage.prompt_tokens},out={usage.completion_tokens}" if usage else "no-usage"
        print(f"[Modal] LLM: {elapsed:.2f}s ({tokens_info}) — {doc['pdf_filename'][:50]}")

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
            if is_locked:
                pn = _extract_policy_number_from_subject(doc.get("email_subject", ""))
                email_hint = doc.get("_password_hint", "")
                return {
                    "provider": _guess_provider(doc.get("email_from", ""), doc.get("email_subject", "")),
                    "policy_number": pn,
                    "password_protected": True,
                    "locked_pdf_path": doc.get("_locked_pdf_path", ""),
                    "password_hint": email_hint or _get_password_hint(doc.get("email_from", ""), ""),
                    "source_pdf": doc["pdf_filename"],
                    "source_email": doc["email_subject"],
                    "source_msg_id": doc.get("_msg_id", ""),
                }
            return None
    except json.JSONDecodeError as e:
        print(f"[Modal] JSON parse error for {doc['pdf_filename']}: {e}")
    except Exception as e:
        print(f"[Modal] LLM error for {doc['pdf_filename']}: {e}")
    return None


@app.function(image=image, timeout=300, cpu=0.25, memory=256)
def process_emails(
    token_json: str,
    user_email: str,
    msg_ids: list[str],
    llm_config: dict,
) -> list[dict]:
    """Orchestrator: fan out PDF downloads + LLM extractions in parallel.
    Routes emails to small (512MB) or large (2GB) containers based on attachment size.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    t_total = time.time()

    # Phase 0: Check attachment sizes to route to correct container tier
    t0 = time.time()
    creds = Credentials.from_authorized_user_info(
        json.loads(token_json),
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds)

    small_ids = []
    large_ids = []

    # Batch fetch metadata to check sizes
    batch_results = {}
    batch_failed = []

    def make_callback(mid):
        def cb(request_id, response, exception):
            if exception:
                batch_failed.append(mid)
            else:
                batch_results[mid] = response
        return cb

    for batch_start in range(0, len(msg_ids), 25):
        batch_chunk = msg_ids[batch_start:batch_start + 25]
        batch = service.new_batch_http_request()
        for mid in batch_chunk:
            batch.add(
                service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["Subject"],
                ),
                callback=make_callback(mid),
            )
        batch.execute()

    # Route based on estimated attachment size from sizeEstimate
    for mid in msg_ids:
        if mid in batch_results:
            size_estimate = batch_results[mid].get("sizeEstimate", 0)
            if size_estimate > LARGE_PDF_THRESHOLD_BYTES:
                large_ids.append(mid)
            else:
                small_ids.append(mid)
        elif mid in batch_failed:
            small_ids.append(mid)  # default to small on error
        else:
            small_ids.append(mid)

    size_elapsed = time.time() - t0
    print(f"[Modal] Phase 0 (size check): {size_elapsed:.2f}s — {len(small_ids)} small, {len(large_ids)} large")

    # Phase 1: Download + extract PDFs in parallel (routed by size)
    t0 = time.time()
    all_docs = []

    # Fan out small emails
    if small_ids:
        for docs in fetch_and_extract_pdf.map(
            [token_json] * len(small_ids),
            small_ids,
            [user_email] * len(small_ids),
        ):
            all_docs.extend(docs)

    # Fan out large emails
    if large_ids:
        for docs in fetch_and_extract_pdf_large.map(
            [token_json] * len(large_ids),
            large_ids,
            [user_email] * len(large_ids),
        ):
            all_docs.extend(docs)

    dl_elapsed = time.time() - t0
    print(f"[Modal] Phase 1 (download+extract): {dl_elapsed:.2f}s — {len(all_docs)} docs from {len(msg_ids)} emails")

    if not all_docs:
        return []

    # Phase 2: LLM extraction in parallel
    t0 = time.time()
    raw_policies = []
    for result in extract_policy.map(
        all_docs,
        [llm_config] * len(all_docs),
    ):
        if result is not None:
            raw_policies.append(result)
    llm_elapsed = time.time() - t0
    print(f"[Modal] Phase 2 (LLM extract): {llm_elapsed:.2f}s — {len(raw_policies)} policies from {len(all_docs)} docs")

    total_elapsed = time.time() - t_total
    print(f"[Modal] TOTAL: {total_elapsed:.2f}s — {len(raw_policies)} policies")
    return raw_policies


# ── Helper functions (used inside Modal containers) ──────


def _find_password_hint(payload: dict) -> str:
    """Extract password hint from email body."""

    text = _extract_body_text_from_payload(payload)
    if text:
        hint = _find_hint_in_text(text)
        if hint:
            return hint

    # Fall back to HTML body
    html = _extract_html_from_payload(payload)
    if html:
        clean = re.sub(r'<[^>]+>', ' ', html)
        clean = re.sub(r'\s+', ' ', clean)
        return _find_hint_in_text(clean)
    return ""


def _find_hint_in_text(text: str) -> str:
    lower = text.lower()
    for marker in ["the password consists", "password is ", "password to view", "password to open"]:
        idx = lower.find(marker)
        if idx >= 0:
            snippet = text[idx:idx + 300].strip()
            for end in [". Or If", ". In case", ". For any", ". If you face"]:
                pos = snippet.find(end)
                if pos > 30:
                    snippet = snippet[:pos + 1]
                    break
            return snippet
    idx = lower.find("password")
    if idx >= 0:
        return text[idx:idx + 200].strip()
    return ""


def _extract_body_text_from_payload(payload: dict) -> str:
    import base64
    text = ""

    def walk(part):
        nonlocal text
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                text += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for p in part.get("parts", []):
            walk(p)

    walk(payload)
    return text


def _extract_html_from_payload(payload: dict) -> str:
    import base64

    def walk(part):
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for p in part.get("parts", []):
            result = walk(p)
            if result:
                return result
        return None

    return walk(payload) or ""


# ── Local entrypoint for testing ─────────────────────────


@app.local_entrypoint()
def main():
    """Quick test: process a single email."""
    import os
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv()

    # Read token from first available token file
    tokens_dir = Path(__file__).parent / "data" / "tokens"
    token_files = list(tokens_dir.glob("*.json"))
    if not token_files:
        print("No token files found in data/tokens/")
        return

    token_path = token_files[0]
    user_email = token_path.stem
    token_json = token_path.read_text()

    print(f"Using token for: {user_email}")
    print(f"Token file: {token_path}")

    # Build LLM config
    if os.getenv("GROQ_API_KEY"):
        llm_config = {
            "api_key": os.getenv("GROQ_API_KEY"),
            "base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.1-8b-instant",
        }
        print("LLM: Groq (Llama 3.1 8B Instant)")
    else:
        llm_config = {
            "api_key": os.getenv("XAI_API_KEY"),
            "base_url": "https://api.x.ai/v1",
            "model": "grok-4-1-fast-non-reasoning",
        }
        print("LLM: xAI Grok")

    # Use the orchestrator to process a few emails
    # First, get some msg_ids via a quick Gmail search
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_info(
        json.loads(token_json),
        ["https://www.googleapis.com/auth/gmail.readonly"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    service = build("gmail", "v1", credentials=creds)
    result = service.users().messages().list(
        userId="me", q='"policy document" has:attachment', maxResults=3
    ).execute()
    messages = result.get("messages", [])
    if not messages:
        print("No emails found matching query")
        return

    msg_ids = [m["id"] for m in messages]
    print(f"\nProcessing {len(msg_ids)} emails via Modal...")

    t0 = time.time()
    policies = process_emails.remote(token_json, user_email, msg_ids, llm_config)
    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(f"Done in {elapsed:.1f}s — extracted {len(policies)} policies")
    for p in policies:
        print(f"  - {p.get('provider', '?')} | {p.get('plan_name', '?')} | {p.get('policy_number', '?')}")
