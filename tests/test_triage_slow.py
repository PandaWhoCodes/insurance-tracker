"""Tests for TriageService with real Groq API.

Marked as slow — excluded from CI by default.
Run with: pytest -m slow
"""

import pytest

from services.triage_service import TriageService


@pytest.fixture(scope="module")
def triage():
    """Module-scoped to avoid reinitializing for each test."""
    return TriageService()


@pytest.mark.slow
class TestGroqTriage:
    @pytest.mark.asyncio
    async def test_policy_email_classified_relevant(self, triage):
        emails = [{
            "msg_id": "1",
            "subject": "Your HDFC ERGO policy copy attached",
            "from": "noreply@hdfcergo.com",
            "snippet": "Please find attached your policy document for Optima Restore",
            "date": "2025-06-01",
            "has_attachments": True,
        }]
        results = await triage.classify_batch_async(emails)
        assert len(results) == 1
        is_relevant, _reason, _score = results[0]
        assert is_relevant is True

    @pytest.mark.asyncio
    async def test_newsletter_classified_not_relevant(self, triage):
        emails = [{
            "msg_id": "2",
            "subject": "Weekly market update - Nifty hits all-time high",
            "from": "newsletter@moneycontrol.com",
            "snippet": "Sensex rallied 500 points today as banking stocks surged",
            "date": "2025-06-01",
            "has_attachments": False,
        }]
        results = await triage.classify_batch_async(emails)
        assert len(results) == 1
        is_relevant, _reason, _score = results[0]
        assert is_relevant is False

    @pytest.mark.asyncio
    async def test_care_health_renewal_classified_relevant(self, triage):
        """The Care Health renewal email that was previously missed by keyword triage."""
        emails = [{
            "msg_id": "3",
            "subject": "Thank you for choosing Care Health Insurance, Mr Thomas Cherian (71562765_20250922)",
            "from": "ahealthystart@careinsurance.com",
            "snippet": "Dear Mr Thomas Cherian, Thank You for trusting us as your preferred Health Insurer.",
            "date": "2025-07-26",
            "has_attachments": True,
        }]
        results = await triage.classify_batch_async(emails)
        assert len(results) == 1
        is_relevant, _reason, _score = results[0]
        assert is_relevant is True

    @pytest.mark.asyncio
    async def test_empty_batch(self, triage):
        results = await triage.classify_batch_async([])
        assert results == []


@pytest.mark.slow
class TestKeywordFallback:
    def test_care_health_with_sender_boost(self, triage):
        """Keyword fallback now recognizes careinsurance.com sender."""
        meta = {
            "subject": "Thank you for choosing Care Health Insurance, Mr Thomas Cherian",
            "from": "ahealthystart@careinsurance.com",
            "snippet": "",
            "has_attachments": True,
        }
        is_relevant, _reason, score = triage._keyword_classify(meta)
        assert is_relevant is True
        assert score >= 0.3

    def test_newsletter_rejected(self, triage):
        meta = {
            "subject": "Daily Trading & Investment Ideas",
            "from": "newsletter@example.com",
            "snippet": "Top stock picks for today",
            "has_attachments": False,
        }
        is_relevant, _reason, _score = triage._keyword_classify(meta)
        assert is_relevant is False

    def test_thank_you_for_choosing_is_strong_positive(self, triage):
        meta = {
            "subject": "Thank you for choosing Some Insurance Company",
            "from": "unknown@someinsurer.com",
            "snippet": "",
            "has_attachments": True,
        }
        is_relevant, _reason, score = triage._keyword_classify(meta)
        assert is_relevant is True
        assert score >= 0.3
