"""Tests for TriageService with real ML model.

Marked as slow — excluded from CI by default.
Run with: pytest -m slow
"""

import pytest

from services.triage_service import TriageService


@pytest.fixture(scope="module")
def triage():
    """Module-scoped to avoid reloading the model for each test."""
    return TriageService()


@pytest.mark.slow
class TestClassifyBatch:
    def test_policy_email_classified_relevant(self, triage):
        emails = [{
            "msg_id": "1",
            "subject": "Your HDFC ERGO policy copy attached",
            "from": "noreply@hdfcergo.com",
            "snippet": "Please find attached your policy document for Optima Restore",
            "date": "2025-06-01",
        }]
        results = triage.classify_batch(emails)
        assert len(results) == 1
        is_relevant, reason, score = results[0]
        assert is_relevant is True

    def test_newsletter_classified_not_relevant(self, triage):
        emails = [{
            "msg_id": "2",
            "subject": "Weekly market update - Nifty hits all-time high",
            "from": "newsletter@moneycontrol.com",
            "snippet": "Sensex rallied 500 points today as banking stocks surged",
            "date": "2025-06-01",
        }]
        results = triage.classify_batch(emails)
        assert len(results) == 1
        is_relevant, reason, score = results[0]
        assert is_relevant is False

    def test_agm_notice_classified_not_relevant(self, triage):
        emails = [{
            "msg_id": "3",
            "subject": "Annual General Meeting Notice - ABC Bank Limited",
            "from": "investor.relations@abcbank.com",
            "snippet": "You are invited to attend the 25th Annual General Meeting of shareholders",
            "date": "2025-06-01",
        }]
        results = triage.classify_batch(emails)
        assert len(results) == 1
        is_relevant, reason, score = results[0]
        assert is_relevant is False

    def test_empty_batch(self, triage):
        results = triage.classify_batch([])
        assert results == []
