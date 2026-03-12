import base64
import re
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
ATTACHMENTS_DIR = BASE_DIR / "attachments"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_search_queries():
    two_years_ago = (datetime.now() - timedelta(days=730)).strftime("%Y/%m/%d")

    # Layer 1: Insurance type keywords (broad net)
    type_terms = [
        "health insurance",
        "mediclaim",
        "car insurance",
        "motor insurance",
        "vehicle insurance",
        "bike insurance",
        "two wheeler insurance",
        "term insurance",
        "term plan",
        "term life",
        "life insurance policy",
        "home insurance",
        "travel insurance",
    ]
    queries = [f"{term} after:{two_years_ago}" for term in type_terms]

    # Layer 2: Policy document indicators (catches ANY insurer's policy emails)
    # These terms appear in actual policy issuance/renewal emails from every Indian insurer
    doc_terms = [
        '"policy document" has:attachment',
        '"policy copy" has:attachment',
        '"policy schedule" has:attachment',
        '"renewed policy" has:attachment',
        '"certificate of insurance" has:attachment',
        '"sum insured"',
        '"policy period"',
        '"policy bond" has:attachment',
        '"premium receipt" has:attachment',
        '"premium paid certificate"',
        'subject:"your policy" has:attachment',
        'subject:"policy renewal"',
        'subject:"renewed" subject:"policy"',
    ]
    queries.extend(f"{q} after:{two_years_ago}" for q in doc_terms)

    # Layer 3: Common Indian insurer email patterns
    queries.append(f'"thank you for choosing" insurance has:attachment after:{two_years_ago}')
    queries.append(f'"your policy" "has been" has:attachment after:{two_years_ago}')
    queries.append(f'"policy number" has:attachment after:{two_years_ago}')

    # Also search for older term life docs without date filter
    queries.append('subject:"policy copy" from:policybazaar')
    queries.append('subject:"term life" has:attachment')
    return queries


class GmailService:
    def __init__(self, user_email: str):
        self.user_email = user_email
        self.token_path = DATA_DIR / f"tokens/{user_email}.json"
        self.user_attachments_dir = ATTACHMENTS_DIR / user_email.replace("@", "_at_")
        self.user_attachments_dir.mkdir(parents=True, exist_ok=True)
        self.service = self._build_service()

    def _build_service(self):
        if not self.token_path.exists():
            raise Exception("No credentials found. Please re-authenticate.")

        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(self.token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise Exception("Credentials expired. Please re-authenticate.")

        return build("gmail", "v1", credentials=creds)

    def fetch_insurance_emails(self) -> list[dict]:
        """Full pipeline: search → fetch details → download PDFs → extract text.
        Returns list of {email_subject, email_from, email_date, pdf_filename, pdf_text}
        """
        metadata = self.fetch_email_metadata()
        results = []
        for i, meta in enumerate(metadata, 1):
            logger.info(f"[{i}/{len(metadata)}] Downloading: {meta['subject'][:50]}")
            docs = self.fetch_document_text(meta["msg_id"])
            results.extend(docs)
        return results

    def fetch_email_metadata(self) -> list[dict]:
        """Step 1: Search Gmail and return lightweight metadata for all emails.
        Uses batch API to fetch metadata in chunks of 25 with retry + backoff.
        Returns list of {msg_id, subject, from, date, snippet}. No PDF download.
        """
        t_total = time.time()
        all_msg_ids = set()
        queries = get_search_queries()

        t_search = time.time()
        for query in queries:
            t0 = time.time()
            messages = self._search_emails(query, max_results=100)
            logger.info(f"[Timing] Search query: {time.time() - t0:.2f}s — '{query[:50]}' → {len(messages)} results")
            all_msg_ids.update(m["id"] for m in messages)
        logger.info(f"[Timing] All searches: {time.time() - t_search:.2f}s — {len(queries)} queries → {len(all_msg_ids)} unique emails")

        if not all_msg_ids:
            return []

        results = {}  # msg_id -> parsed metadata
        remaining = list(all_msg_ids)
        batch_size = 25
        max_retries = 5

        t_batch_phase = time.time()
        for attempt in range(max_retries + 1):
            if not remaining:
                break

            if attempt > 0:
                delay = min(2 ** attempt, 30)
                logger.info(f"Retry {attempt}/{max_retries}: {len(remaining)} remaining, waiting {delay}s...")
                time.sleep(delay)

            failed = []

            for batch_start in range(0, len(remaining), batch_size):
                batch_chunk = remaining[batch_start:batch_start + batch_size]
                batch_results = {}
                batch_failed = []

                def make_callback(mid):
                    def cb(request_id, response, exception):
                        if exception:
                            batch_failed.append(mid)
                            return
                        batch_results[mid] = response
                    return cb

                t0 = time.time()
                batch = self.service.new_batch_http_request()
                for mid in batch_chunk:
                    batch.add(
                        self.service.users().messages().get(
                            userId="me", id=mid, format="metadata",
                            metadataHeaders=["Subject", "From", "Date"],
                        ),
                        callback=make_callback(mid),
                    )
                batch.execute()
                logger.info(f"[Timing] Metadata batch API: {time.time() - t0:.2f}s — {len(batch_chunk)} emails, {len(batch_failed)} failed")

                for mid, msg in batch_results.items():
                    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                    # Check for attachments in payload parts
                    parts = msg.get("payload", {}).get("parts", [])
                    has_att = any(
                        p.get("filename") for p in parts
                    ) if parts else False
                    results[mid] = {
                        "msg_id": mid,
                        "subject": headers.get("Subject", "(no subject)"),
                        "from": headers.get("From", ""),
                        "date": headers.get("Date", ""),
                        "snippet": msg.get("snippet", ""),
                        "has_attachments": has_att,
                    }

                failed.extend(batch_failed)

                # Brief pause between batches to avoid rate limits
                if batch_start + batch_size < len(remaining):
                    time.sleep(0.3)

            logger.info(f"Attempt {attempt + 1}: fetched {len(results)}/{len(all_msg_ids)} metadata, {len(failed)} failed")
            remaining = failed

        logger.info(f"[Timing] Batch metadata phase: {time.time() - t_batch_phase:.2f}s")

        if remaining:
            logger.warning(f"Could not fetch metadata for {len(remaining)} emails after {max_retries + 1} attempts")

        logger.info(f"[Timing] FETCH_EMAIL_METADATA TOTAL: {time.time() - t_total:.2f}s — {len(results)} emails")
        return list(results.values())

    def fetch_document_text(self, msg_id: str) -> list[dict]:
        """Step 2: For a single message, download PDFs and extract text.
        Returns list of {email_subject, email_from, email_date, pdf_filename, pdf_text}.
        """
        t_total = time.time()
        try:
            t0 = time.time()
            detail = self._get_message_detail(msg_id)
            logger.info(f"[Timing] _get_message_detail: {time.time() - t0:.2f}s — {detail['subject'][:50]}")
        except Exception as e:
            logger.warning(f"Failed to fetch message {msg_id}: {e}")
            return []

        results = []

        # Download attachments
        parts = detail["payload"].get("parts", [])
        if not parts and detail["payload"].get("body"):
            parts = [detail["payload"]]

        t0 = time.time()
        downloaded = self._download_attachments(msg_id, parts, detail["subject"])
        logger.info(f"[Timing] _download_attachments: {time.time() - t0:.2f}s — {len(downloaded)} files")

        for filepath in downloaded:
            if filepath.lower().endswith(".pdf"):
                t0 = time.time()
                pdf_text, is_locked = self._extract_text_from_pdf(filepath)
                pdf_elapsed = time.time() - t0
                file_size_kb = Path(filepath).stat().st_size / 1024
                logger.info(
                    f"[Timing] _extract_text_from_pdf: {pdf_elapsed:.2f}s — "
                    f"{Path(filepath).name} — {file_size_kb:.0f}KB — "
                    f"{'LOCKED' if is_locked else f'{len(pdf_text)} chars'}"
                )
                if pdf_text and len(pdf_text.strip()) > 100:
                    results.append({
                        "email_subject": detail["subject"],
                        "email_from": detail["from"],
                        "email_date": detail["date"],
                        "pdf_filename": Path(filepath).name,
                        "pdf_text": pdf_text,
                    })
                elif is_locked:
                    hint = self._extract_password_hint(detail["payload"])
                    results.append({
                        "email_subject": detail["subject"],
                        "email_from": detail["from"],
                        "email_date": detail["date"],
                        "pdf_filename": Path(filepath).name,
                        "pdf_text": f"[PASSWORD-PROTECTED PDF]\nFilename: {Path(filepath).name}\nEmail subject: {detail['subject']}",
                        "_password_protected": True,
                        "_locked_pdf_path": filepath,
                        "_password_hint": hint,
                    })

        # Also check email body for policy info (some come without PDFs)
        t0 = time.time()
        body_text = self._extract_body_text(detail["payload"])
        body_elapsed = time.time() - t0
        if body_elapsed > 0.1:
            logger.info(f"[Timing] _extract_body_text: {body_elapsed:.2f}s — {len(body_text)} chars")
        if body_text and len(body_text.strip()) > 200:
            body_lower = body_text.lower()
            if any(kw in body_lower for kw in [
                "policy no", "policy number", "sum insured", "premium",
                "policy period", "insured value"
            ]):
                results.append({
                    "email_subject": detail["subject"],
                    "email_from": detail["from"],
                    "email_date": detail["date"],
                    "pdf_filename": f"email_body_{msg_id}",
                    "pdf_text": body_text[:10000],
                })

        logger.info(f"[Timing] FETCH_DOCUMENT_TEXT TOTAL: {time.time() - t_total:.2f}s — {len(results)} docs from {detail['subject'][:50]}")
        return results

    def _search_emails(self, query: str, max_results: int = 50) -> list[dict]:
        messages = []
        try:
            t0 = time.time()
            result = self.service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            logger.info(f"[Timing] Gmail list API: {time.time() - t0:.2f}s — '{query[:40]}'")
            messages = result.get("messages", [])

            page = 1
            while "nextPageToken" in result and len(messages) < max_results:
                t0 = time.time()
                result = self.service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=max_results - len(messages),
                    pageToken=result["nextPageToken"],
                ).execute()
                page += 1
                logger.info(f"[Timing] Gmail list API page {page}: {time.time() - t0:.2f}s")
                messages.extend(result.get("messages", []))
        except Exception as e:
            logger.error(f"Error searching for '{query}': {e}")

        return messages

    def _get_message_detail(self, msg_id: str) -> dict:
        t0 = time.time()
        msg = self.service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
        logger.info(f"[Timing] Gmail get(full) API: {time.time() - t0:.2f}s — msg_id={msg_id[:12]}")

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

        return {
            "id": msg_id,
            "subject": headers.get("Subject", "(no subject)"),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
            "payload": msg["payload"],
        }

    def _download_attachments(self, msg_id: str, parts: list, email_subject: str) -> list[str]:
        t_total = time.time()
        downloaded = []

        def process_parts(parts_list):
            for part in parts_list:
                filename = part.get("filename", "")
                mime_type = part.get("mimeType", "")

                if part.get("parts"):
                    process_parts(part["parts"])

                if not filename:
                    continue

                # Only PDFs
                if not (
                    mime_type.startswith("application/pdf")
                    or mime_type.startswith("application/octet-stream")
                    or filename.lower().endswith(".pdf")
                ):
                    continue

                body = part.get("body", {})
                attachment_id = body.get("attachmentId")
                if not attachment_id:
                    continue

                # Gmail reports estimated size in the part body
                est_size = body.get("size", 0)
                est_kb = est_size / 1024 if est_size else 0

                try:
                    t0 = time.time()
                    att = self.service.users().messages().attachments().get(
                        userId="me", messageId=msg_id, id=attachment_id
                    ).execute()
                    api_elapsed = time.time() - t0

                    t_decode = time.time()
                    data = base64.urlsafe_b64decode(att["data"])
                    decode_elapsed = time.time() - t_decode
                    actual_kb = len(data) / 1024

                    logger.info(
                        f"[Timing] Attachment API: {api_elapsed:.2f}s, decode: {decode_elapsed:.2f}s — "
                        f"{filename} — est:{est_kb:.0f}KB actual:{actual_kb:.0f}KB ({actual_kb/1024:.1f}MB)"
                    )

                    # Ensure PDF files have .pdf extension
                    if mime_type.startswith("application/pdf") and not filename.lower().endswith(".pdf"):
                        filename = filename + ".pdf"

                    # Sanitize filename — replace path separators and special chars
                    safe_filename_part = re.sub(r'[/\\]', '_', filename)
                    safe_subject = re.sub(r'[^\w\s-]', '', email_subject)[:50].strip()
                    safe_filename = f"{safe_subject}__{safe_filename_part}"
                    filepath = self.user_attachments_dir / safe_filename

                    counter = 1
                    while filepath.exists():
                        filepath = self.user_attachments_dir / f"{filepath.stem}_{counter}{filepath.suffix}"
                        counter += 1

                    with open(filepath, "wb") as f:
                        f.write(data)

                    downloaded.append(str(filepath))
                    logger.info(f"Saved: {filepath.name}")
                except Exception as e:
                    logger.warning(f"Error downloading {filename} (est:{est_kb:.0f}KB): {e}")

        process_parts(parts)
        logger.info(f"[Timing] _download_attachments TOTAL: {time.time() - t_total:.2f}s — {len(downloaded)} files saved")
        return downloaded

    def redownload_attachment(self, msg_id: str) -> list[str]:
        """Re-download PDF attachments for a specific message. Returns list of file paths."""
        try:
            detail = self._get_message_detail(msg_id)
            parts = detail["payload"].get("parts", [detail["payload"]])
            return self._download_attachments(msg_id, parts, detail["subject"])
        except Exception as e:
            logger.warning(f"Failed to re-download attachment for msg_id={msg_id}: {e}")
            return []

    def _extract_text_from_pdf(self, filepath: str) -> tuple[str, bool]:
        """Extract text from PDF using PyMuPDF. Returns (text, is_password_protected)."""
        text = ""
        fname = Path(filepath).name
        try:
            t_open = time.time()
            doc = fitz.open(filepath)
            open_elapsed = time.time() - t_open
            num_pages = len(doc)

            if doc.is_encrypted:
                doc.close()
                logger.warning(f"Password-protected PDF (cannot extract): {fname}")
                return "", True

            logger.info(f"[Timing] fitz.open: {open_elapsed:.3f}s — {fname} — {num_pages} pages")
            for i, page in enumerate(doc):
                t_page = time.time()
                page_text = page.get_text()
                page_elapsed = time.time() - t_page
                if page_elapsed > 0.5:
                    logger.info(f"[Timing] fitz page {i+1}/{num_pages}: {page_elapsed:.2f}s — {fname}")
                if page_text:
                    text += page_text + "\n"
            doc.close()
        except Exception as e:
            err_name = type(e).__name__
            err_repr = repr(e).lower()
            if "password" in err_repr or "encrypted" in err_repr:
                logger.warning(f"Password-protected PDF (cannot extract): {fname}")
                return "", True
            else:
                logger.warning(f"Error reading PDF {filepath}: {err_name}: {e}")
        return text, False

    def _extract_password_hint(self, payload: dict) -> str:
        """Extract password hint from email body (plain text or HTML)."""
        # Try plain text first
        plain = self._extract_body_text(payload)
        hint = self._find_hint_in_text(plain) if plain else ""
        if hint:
            return hint

        # Fall back to HTML body
        html = self._extract_html_body(payload)
        if html:
            # Strip HTML tags
            clean = re.sub(r'<[^>]+>', ' ', html)
            clean = re.sub(r'\s+', ' ', clean)
            return self._find_hint_in_text(clean)
        return ""

    def _find_hint_in_text(self, text: str) -> str:
        """Find password-related hint in text."""
        lower = text.lower()
        # Look for the most specific password instruction
        for marker in ["the password consists", "password is ", "password to view", "password to open"]:
            idx = lower.find(marker)
            if idx >= 0:
                snippet = text[idx:idx + 300].strip()
                # Truncate at example end or noise
                for end in [". Or If", ". In case", ". For any", ". If you face"]:
                    pos = snippet.find(end)
                    if pos > 30:
                        snippet = snippet[:pos + 1]
                        break
                return snippet
        # Generic fallback — grab around "password"
        idx = lower.find("password")
        if idx >= 0:
            snippet = text[idx:idx + 200].strip()
            return snippet
        return ""

    def _extract_html_body(self, payload: dict) -> str:
        """Extract HTML body from email payload."""
        def walk(part):
            mime = part.get("mimeType", "")
            if mime == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            for p in part.get("parts", []):
                result = walk(p)
                if result:
                    return result
            return None
        return walk(payload) or ""

    def _extract_body_text(self, payload: dict) -> str:
        text = ""

        def walk_parts(part):
            nonlocal text
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    text += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if part.get("parts"):
                for p in part["parts"]:
                    walk_parts(p)

        walk_parts(payload)
        return text
