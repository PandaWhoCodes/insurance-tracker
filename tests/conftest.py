import base64
import pytest
from unittest.mock import patch


def _make_policy(**overrides):
    """Factory for policy dicts with sensible defaults."""
    base = {
        "policy_number": "TEST123456",
        "type": "health",
        "provider": "Test Insurance Co",
        "plan_name": "Test Plan",
        "insured_members": [{"name": "Test User", "relationship": "Self", "date_of_birth": None}],
        "sum_insured": 500000,
        "premium": 10000,
        "premium_frequency": "yearly",
        "policy_start": "2025-01-01",
        "policy_end": "2027-01-01",
        "status": "ACTIVE",
        "vehicle": None,
        "nominee": None,
        "intermediary": None,
        "coverages": None,
        "notes": None,
        "source_pdf": "test.pdf",
        "source_email": "test email",
    }
    base.update(overrides)
    return base


@pytest.fixture
def active_policy():
    return _make_policy(
        policy_number="2805205442589302000",
        provider="HDFC ERGO General Insurance",
        plan_name="Optima Restore Individual",
        sum_insured=1500000,
        premium=15621,
        policy_start="2025-05-23",
        policy_end="2027-05-22",
        status="ACTIVE",
    )


@pytest.fixture
def expired_policy():
    return _make_policy(
        policy_number="2805205442589301000",
        provider="HDFC ERGO General Insurance",
        plan_name="Optima Restore Individual",
        sum_insured=None,
        premium=14000,
        policy_start="2024-05-23",
        policy_end="2024-05-22",
        status="EXPIRED",
    )


@pytest.fixture
def lic_policy():
    return _make_policy(
        policy_number="123456789",
        type="term_life",
        provider="LIC of India",
        plan_name="945",
        sum_insured=5000000,
        premium=8000,
        policy_start="2023-01-01",
        policy_end="2050-01-01",
        nominee={"name": "Test Spouse", "relationship": "Spouse"},
    )


@pytest.fixture
def locked_policy():
    return _make_policy(
        policy_number="LOCK123",
        provider="ICICI Lombard",
        plan_name=None,
        sum_insured=None,
        premium=None,
        password_protected=True,
        locked_pdf_path="/tmp/test.pdf",
        password_hint="Your date of birth in DDMMYYYY format",
    )


@pytest.fixture
def car_policy_pair():
    """Two Acko policies with /01 and /02 suffixes that should dedup."""
    p1 = _make_policy(
        policy_number="DCAR10148431569/01",
        type="car",
        provider="ACKO General Insurance",
        plan_name="Comprehensive",
        sum_insured=147185,
        premium=5353,
        policy_start="2024-02-23",
        policy_end="2025-02-22",
        status="EXPIRED",
        vehicle={"make": "Renault", "model": "Kwid", "registration": "TN14V7880"},
    )
    p2 = _make_policy(
        policy_number="DCAR10148431569/02",
        type="car",
        provider="ACKO General Insurance",
        plan_name="Comprehensive - Super Saver",
        sum_insured=160000,
        premium=6000,
        policy_start="2025-02-23",
        policy_end="2027-02-22",
        status="ACTIVE",
        vehicle={"make": "Renault", "model": "Kwid", "registration": "TN14V7880"},
        nominee={"name": "Test Brother", "relationship": "Brother"},
    )
    return p1, p2


@pytest.fixture
def email_metadata_batch():
    return [
        {
            "msg_id": "msg_001",
            "subject": "Your HDFC ERGO policy copy attached",
            "from": "noreply@hdfcergo.com",
            "snippet": "Please find attached your policy document",
            "date": "2025-06-01",
        },
        {
            "msg_id": "msg_002",
            "subject": "Weekly market update",
            "from": "newsletter@moneycontrol.com",
            "snippet": "Nifty rallied 2% this week",
            "date": "2025-06-01",
        },
        {
            "msg_id": "msg_003",
            "subject": "Renew your car insurance now!",
            "from": "marketing@policybazaar.com",
            "snippet": "Compare and save on your motor insurance",
            "date": "2025-06-01",
        },
    ]


@pytest.fixture
def mime_payload_plain():
    text = "Your policy password is your date of birth in DDMMYYYY format."
    encoded = base64.urlsafe_b64encode(text.encode()).decode()
    return {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }


@pytest.fixture
def mime_payload_html():
    html = "<html><body><p>The password consists of four digits of your DOB</p></body></html>"
    encoded = base64.urlsafe_b64encode(html.encode()).decode()
    return {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": ""}},
            {"mimeType": "text/html", "body": {"data": encoded}},
        ],
    }


@pytest.fixture
def mime_payload_multipart():
    plain = "Policy number: 12345. Sum insured: 500000."
    html = "<html><body><p>Password to open the document is your DOB</p></body></html>"
    return {
        "mimeType": "multipart/alternative",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(plain.encode()).decode()}},
            {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}},
        ],
    }


@pytest.fixture
def make_pipeline():
    """Create a PipelineService with TriageService and AsyncOpenAI patched out."""
    def _factory():
        with patch("services.pipeline_service.TriageService"):
            with patch("services.pipeline_service.AsyncOpenAI"):
                from services.pipeline_service import PipelineService
                return PipelineService()
    return _factory
