"""Tests for helper methods in services/gmail_service.py."""

import base64
from unittest.mock import patch

from services.gmail_service import GmailService, get_search_queries


def _make_gmail_service():
    """Create GmailService without calling __init__ (no OAuth needed)."""
    with patch.object(GmailService, '__init__', lambda self, *a, **kw: None):
        return GmailService.__new__(GmailService)


class TestGetSearchQueries:
    def test_contains_insurance_terms(self):
        queries = get_search_queries()
        combined = " ".join(queries).lower()
        assert "health insurance" in combined
        assert "car insurance" in combined
        assert "term insurance" in combined or "term life" in combined
        assert "bike insurance" in combined

    def test_has_date_filter(self):
        queries = get_search_queries()
        # Most queries should have 'after:' date filter
        dated = [q for q in queries if "after:" in q]
        assert len(dated) >= 5

    def test_returns_list(self):
        queries = get_search_queries()
        assert isinstance(queries, list)
        assert len(queries) > 0


class TestExtractBodyText:
    def test_plain_text(self, mime_payload_plain):
        svc = _make_gmail_service()
        text = svc._extract_body_text(mime_payload_plain)
        assert "password" in text.lower()
        assert "DDMMYYYY" in text

    def test_multipart(self, mime_payload_multipart):
        svc = _make_gmail_service()
        text = svc._extract_body_text(mime_payload_multipart)
        assert "Policy number" in text

    def test_empty_payload(self):
        svc = _make_gmail_service()
        payload = {"mimeType": "text/plain", "body": {}}
        text = svc._extract_body_text(payload)
        assert text == ""


class TestExtractHtmlBody:
    def test_finds_html(self, mime_payload_html):
        svc = _make_gmail_service()
        html = svc._extract_html_body(mime_payload_html)
        assert "<p>" in html
        assert "password" in html.lower()

    def test_nested_multipart(self):
        svc = _make_gmail_service()
        inner_html = "<html><body><p>nested content</p></body></html>"
        encoded = base64.urlsafe_b64encode(inner_html.encode()).decode()
        payload = {
            "mimeType": "multipart/mixed",
            "body": {},
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "body": {},
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": encoded}},
                    ],
                }
            ],
        }
        html = svc._extract_html_body(payload)
        assert "nested content" in html

    def test_no_html(self):
        svc = _make_gmail_service()
        payload = {"mimeType": "text/plain", "body": {"data": ""}}
        html = svc._extract_html_body(payload)
        assert html == ""


class TestFindHintInText:
    def test_password_is(self):
        svc = _make_gmail_service()
        text = "Your policy is ready. The password is your date of birth in DDMMYYYY format."
        hint = svc._find_hint_in_text(text)
        assert "password is" in hint.lower()

    def test_password_consists(self):
        svc = _make_gmail_service()
        text = "The password consists of four digits. Please enter the last four digits of your registration number."
        hint = svc._find_hint_in_text(text)
        assert "password consists" in hint.lower()

    def test_password_to_view(self):
        svc = _make_gmail_service()
        text = "The password to view your document is your DOB."
        hint = svc._find_hint_in_text(text)
        assert len(hint) > 0

    def test_no_password_marker(self):
        svc = _make_gmail_service()
        text = "Thank you for choosing our insurance. Your policy is attached."
        hint = svc._find_hint_in_text(text)
        assert hint == ""

    def test_generic_password_fallback(self):
        svc = _make_gmail_service()
        text = "Your document is protected with a password for security."
        hint = svc._find_hint_in_text(text)
        assert "password" in hint.lower()


class TestExtractPasswordHint:
    def test_prefers_plain_text(self):
        svc = _make_gmail_service()
        plain_text = "The password is your DOB in DDMMYYYY format."
        html = "<p>Some unrelated HTML content without password info</p>"
        encoded_plain = base64.urlsafe_b64encode(plain_text.encode()).decode()
        encoded_html = base64.urlsafe_b64encode(html.encode()).decode()
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded_plain}},
                {"mimeType": "text/html", "body": {"data": encoded_html}},
            ],
        }
        hint = svc._extract_password_hint(payload)
        assert "password is" in hint.lower()

    def test_falls_back_to_html(self, mime_payload_html):
        svc = _make_gmail_service()
        hint = svc._extract_password_hint(mime_payload_html)
        assert "password" in hint.lower()

    def test_empty_payload(self):
        svc = _make_gmail_service()
        payload = {"mimeType": "text/plain", "body": {}}
        hint = svc._extract_password_hint(payload)
        assert hint == ""
