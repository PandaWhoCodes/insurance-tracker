"""
Fetch insurance-related emails and attachments from Gmail.
Parses PDFs and extracts policy information.
"""

import os
import sys
import json
import base64
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import pdfplumber

load_dotenv()

BASE_DIR = Path(__file__).parent
ATTACHMENTS_DIR = BASE_DIR / "attachments"
DATA_DIR = BASE_DIR / "data"
TOKEN_FILE = BASE_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

SEARCH_QUERIES = [
    "health insurance after:2025/02/28",
    "mediclaim after:2025/02/28",
    "car insurance after:2025/02/28",
    "motor insurance after:2025/02/28",
    "vehicle insurance after:2025/02/28",
    "term insurance after:2025/02/28",
    "term plan after:2025/02/28",
    "term life after:2025/02/28",
]


def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_config = {
                "web": {
                    "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                    "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost:8080/"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(
                port=8080,
                redirect_uri_trailing_slash=False,
            )

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def search_emails(service, query, max_results=50):
    """Search Gmail for messages matching query."""
    messages = []
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        messages = result.get("messages", [])

        while "nextPageToken" in result and len(messages) < max_results:
            result = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results - len(messages),
                pageToken=result["nextPageToken"],
            ).execute()
            messages.extend(result.get("messages", []))

    except Exception as e:
        print(f"  Error searching for '{query}': {e}")

    return messages


def get_message_detail(service, msg_id):
    """Get full message details including headers and body."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

    return {
        "id": msg_id,
        "subject": headers.get("Subject", "(no subject)"),
        "from": headers.get("From", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "payload": msg["payload"],
        "labelIds": msg.get("labelIds", []),
    }


def download_attachments(service, msg_id, parts, email_subject):
    """Download attachments from an email message."""
    downloaded = []

    def process_parts(parts_list):
        for part in parts_list:
            filename = part.get("filename", "")
            mime_type = part.get("mimeType", "")

            # Recurse into multipart
            if part.get("parts"):
                process_parts(part["parts"])

            if not filename:
                continue

            # Only download PDFs and images
            if not any(
                mime_type.startswith(t)
                for t in ["application/pdf", "image/", "application/octet-stream"]
            ) and not filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
                continue

            attachment_id = part.get("body", {}).get("attachmentId")
            if not attachment_id:
                continue

            try:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=attachment_id
                ).execute()

                data = base64.urlsafe_b64decode(att["data"])

                # Sanitize filename
                safe_subject = re.sub(r'[^\w\s-]', '', email_subject)[:50].strip()
                safe_filename = f"{safe_subject}__{filename}"
                filepath = ATTACHMENTS_DIR / safe_filename

                # Avoid overwriting
                counter = 1
                while filepath.exists():
                    stem = filepath.stem
                    filepath = ATTACHMENTS_DIR / f"{stem}_{counter}{filepath.suffix}"
                    counter += 1

                with open(filepath, "wb") as f:
                    f.write(data)

                downloaded.append(str(filepath))
                print(f"    Saved: {filepath.name}")

            except Exception as e:
                print(f"    Error downloading {filename}: {e}")

    process_parts(parts)
    return downloaded


def extract_text_from_pdf(filepath):
    """Extract text from a PDF file."""
    text = ""
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"  Error reading PDF {filepath}: {e}")
    return text


def extract_body_text(payload):
    """Extract plain text body from email payload."""
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


def main():
    print("=" * 60)
    print("Insurance Email & Document Fetcher")
    print("=" * 60)

    # Authenticate
    print("\nAuthenticating with Gmail...")
    service = get_gmail_service()
    print("Authenticated successfully!\n")

    # Collect unique message IDs across all search queries
    all_msg_ids = set()
    for query in SEARCH_QUERIES:
        print(f"Searching: '{query}'...")
        messages = search_emails(service, query, max_results=30)
        new_ids = {m["id"] for m in messages} - all_msg_ids
        if new_ids:
            print(f"  Found {len(new_ids)} new results")
        all_msg_ids.update(m["id"] for m in messages)

    print(f"\nTotal unique emails found: {len(all_msg_ids)}")

    if not all_msg_ids:
        print("No insurance-related emails found.")
        return

    # Fetch details and attachments
    all_emails = []
    all_attachments = []

    for i, msg_id in enumerate(all_msg_ids, 1):
        print(f"\n[{i}/{len(all_msg_ids)}] Fetching email...")
        detail = get_message_detail(service, msg_id)
        print(f"  Subject: {detail['subject']}")
        print(f"  From: {detail['from']}")
        print(f"  Date: {detail['date']}")

        # Extract body text
        body_text = extract_body_text(detail["payload"])

        # Download attachments
        parts = detail["payload"].get("parts", [])
        if not parts and detail["payload"].get("body"):
            parts = [detail["payload"]]

        downloaded = download_attachments(service, msg_id, parts, detail["subject"])
        all_attachments.extend(downloaded)

        # Extract text from PDF attachments
        pdf_texts = {}
        for att_path in downloaded:
            if att_path.lower().endswith(".pdf"):
                print(f"  Extracting text from: {Path(att_path).name}")
                pdf_text = extract_text_from_pdf(att_path)
                if pdf_text:
                    pdf_texts[Path(att_path).name] = pdf_text

        email_record = {
            "id": msg_id,
            "subject": detail["subject"],
            "from": detail["from"],
            "date": detail["date"],
            "snippet": detail["snippet"],
            "body_text": body_text[:5000],  # Truncate very long bodies
            "attachments": downloaded,
            "pdf_texts": pdf_texts,
        }
        all_emails.append(email_record)

    # Save all data
    output_file = DATA_DIR / "insurance_emails.json"
    with open(output_file, "w") as f:
        json.dump(
            {
                "fetched_at": datetime.now().isoformat(),
                "total_emails": len(all_emails),
                "total_attachments": len(all_attachments),
                "emails": all_emails,
            },
            f,
            indent=2,
            default=str,
        )

    print(f"\n{'=' * 60}")
    print(f"Done! Saved {len(all_emails)} emails to {output_file}")
    print(f"Downloaded {len(all_attachments)} attachments to {ATTACHMENTS_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
