"""Tests for TriageService with mocked Groq API."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.triage_service import TriageService


def _make_triage(with_groq=True):
    """Create TriageService with Groq client mocked."""
    with patch.dict("os.environ", {"GROQ_API_KEY": "test"} if with_groq else {}, clear=False):
        with patch("services.triage_service.AsyncOpenAI") as mock_cls:
            ts = TriageService()
            if with_groq:
                ts._client = mock_cls.return_value
            return ts


def _mock_groq_response(content: str):
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = content
    mock_resp.usage = MagicMock(prompt_tokens=500, completion_tokens=20)
    return mock_resp


class TestGroqBatchClassify:
    @pytest.mark.asyncio
    async def test_basic_yes_no(self):
        ts = _make_triage()
        ts._client.chat.completions.create = AsyncMock(
            return_value=_mock_groq_response("1. YES\n2. NO\n3. YES")
        )

        emails = [
            {"subject": "Policy copy attached", "from": "a@b.com", "snippet": "", "has_attachments": True},
            {"subject": "Newsletter", "from": "c@d.com", "snippet": "", "has_attachments": False},
            {"subject": "Thank you for choosing insurance", "from": "e@f.com", "snippet": "", "has_attachments": True},
        ]
        results = await ts.classify_batch_async(emails)

        assert len(results) == 3
        assert results[0][0] is True   # YES
        assert results[1][0] is False  # NO
        assert results[2][0] is True   # YES
        assert results[0][1] == "groq:yes"
        assert results[1][1] == "groq:no"

    @pytest.mark.asyncio
    async def test_empty_batch(self):
        ts = _make_triage()
        results = await ts.classify_batch_async([])
        assert results == []

    @pytest.mark.asyncio
    async def test_falls_back_on_missing_lines(self):
        """If Groq returns fewer lines than emails, missing ones use keyword fallback."""
        ts = _make_triage()
        ts._client.chat.completions.create = AsyncMock(
            return_value=_mock_groq_response("1. YES")
        )

        emails = [
            {"subject": "Policy document", "from": "a@b.com", "snippet": "", "has_attachments": True},
            {"subject": "Newsletter weekly", "from": "c@d.com", "snippet": "stocks", "has_attachments": False},
        ]
        results = await ts.classify_batch_async(emails)

        assert len(results) == 2
        assert results[0][0] is True      # from Groq
        assert results[0][1] == "groq:yes"
        # Second one falls back to keyword
        assert "keyword" in results[1][1]

    @pytest.mark.asyncio
    async def test_falls_back_on_groq_error(self):
        """If Groq API fails, entire batch uses keyword fallback."""
        ts = _make_triage()
        ts._client.chat.completions.create = AsyncMock(
            side_effect=Exception("API error")
        )

        emails = [
            {"subject": "Your policy copy", "from": "noreply@hdfcergo.com", "snippet": "policy", "has_attachments": True},
        ]
        results = await ts.classify_batch_async(emails)

        assert len(results) == 1
        assert "keyword" in results[0][1]

    @pytest.mark.asyncio
    async def test_no_groq_uses_keyword(self):
        """Without GROQ_API_KEY, uses keyword fallback."""
        ts = _make_triage(with_groq=False)

        emails = [
            {"subject": "Policy document attached", "from": "noreply@hdfcergo.com", "snippet": "", "has_attachments": True},
        ]
        results = await ts.classify_batch_async(emails)

        assert len(results) == 1
        assert "keyword" in results[0][1]
        assert results[0][0] is True  # strong positive + sender

    @pytest.mark.asyncio
    async def test_batching(self):
        """Emails are split into batches of BATCH_SIZE."""
        ts = _make_triage()
        ts._client.chat.completions.create = AsyncMock(
            side_effect=[
                _mock_groq_response("\n".join(f"{i+1}. YES" for i in range(30))),
                _mock_groq_response("\n".join(f"{i+1}. NO" for i in range(5))),
            ]
        )

        emails = [
            {"subject": f"Email {i}", "from": "a@b.com", "snippet": "", "has_attachments": False}
            for i in range(35)
        ]
        results = await ts.classify_batch_async(emails)

        assert len(results) == 35
        assert ts._client.chat.completions.create.await_count == 2
        # First 30 should be YES, last 5 NO
        assert all(r[0] for r in results[:30])
        assert not any(r[0] for r in results[30:])

    @pytest.mark.asyncio
    async def test_numbered_parsing_ignores_extra_text(self):
        """Parser handles extra text around numbers."""
        ts = _make_triage()
        ts._client.chat.completions.create = AsyncMock(
            return_value=_mock_groq_response("1. YES - policy document\n2. NO - newsletter\n")
        )

        emails = [
            {"subject": "Policy", "from": "a@b.com", "snippet": "", "has_attachments": True},
            {"subject": "News", "from": "c@d.com", "snippet": "", "has_attachments": False},
        ]
        results = await ts.classify_batch_async(emails)
        assert results[0][0] is True
        assert results[1][0] is False


class TestKeywordClassify:
    def test_strong_positive_in_subject(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Your policy document is attached", "from": "", "snippet": "", "has_attachments": False}
        is_rel, reason, score = ts._keyword_classify(meta)
        assert is_rel is True
        assert score >= 0.3

    def test_thank_you_for_choosing_strong_positive(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Thank you for choosing Care Health Insurance", "from": "ahealthystart@careinsurance.com", "snippet": "", "has_attachments": True}
        is_rel, reason, score = ts._keyword_classify(meta)
        assert is_rel is True
        assert score >= 0.7  # strong_pos(0.4) + sender(0.2) + attachment(0.15)

    def test_careinsurance_sender_recognized(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Some insurance email", "from": "ahealthystart@careinsurance.com", "snippet": "", "has_attachments": False}
        is_rel, reason, score = ts._keyword_classify(meta)
        # sender boost should be applied
        assert score >= 0.2

    def test_newsletter_rejected(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Daily Trading & Investment Ideas", "from": "news@example.com", "snippet": "top stocks mutual fund sip", "has_attachments": False}
        is_rel, reason, score = ts._keyword_classify(meta)
        assert is_rel is False
        assert score < 0.3

    def test_negative_keywords_reduce_score(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Special offer on health insurance", "from": "", "snippet": "buy now compare plans discount", "has_attachments": False}
        is_rel, reason, score = ts._keyword_classify(meta)
        # Has weak positive (health insurance) but multiple negatives
        assert score < 0.3

    def test_attachment_boost(self):
        ts = _make_triage(with_groq=False)
        meta = {"subject": "Some email", "from": "", "snippet": "", "has_attachments": True}
        _, _, with_att = ts._keyword_classify(meta)
        meta["has_attachments"] = False
        _, _, without_att = ts._keyword_classify(meta)
        assert with_att > without_att


class TestHasAttachment:
    def test_has_attachments_flag(self):
        ts = _make_triage(with_groq=False)
        assert ts._has_attachment({"has_attachments": True}) is True
        assert ts._has_attachment({"has_attachments": False}) is False

    def test_pdf_in_attachments_list(self):
        ts = _make_triage(with_groq=False)
        assert ts._has_attachment({"attachments": ["doc.pdf"]}) is True
        assert ts._has_attachment({"attachments": ["doc.txt"]}) is False

    def test_dict_attachments(self):
        ts = _make_triage(with_groq=False)
        assert ts._has_attachment({"attachments": [{"filename": "policy.pdf"}]}) is True
        assert ts._has_attachment({"attachments": [{"filename": "image.png"}]}) is False

    def test_pdf_texts_fallback(self):
        ts = _make_triage(with_groq=False)
        assert ts._has_attachment({"pdf_texts": ["some text"]}) is True
        assert ts._has_attachment({}) is False


class TestFormatEmail:
    def test_formats_correctly(self):
        ts = _make_triage()
        meta = {
            "subject": "Test Subject",
            "from": "test@example.com",
            "snippet": "Test snippet text",
            "has_attachments": True,
        }
        formatted = ts._format_email(1, meta)
        assert "1." in formatted
        assert "Test Subject" in formatted
        assert "test@example.com" in formatted
        assert "Yes" in formatted  # has_attachments
