"""Tests for async pipeline methods with mocked externals."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_pipeline_service():
    """Create PipelineService with external deps patched out."""
    with patch("services.pipeline_service.TriageService") as mock_triage_cls:
        with patch("services.pipeline_service.AsyncOpenAI") as mock_openai_cls:
            from services.pipeline_service import PipelineService
            ps = PipelineService()
            ps._triage = mock_triage_cls.return_value
            ps.client = mock_openai_cls.return_value
            return ps


class TestTriage:
    @pytest.mark.asyncio
    async def test_skips_known_msg_ids(self):
        pipeline = _make_pipeline_service()
        pipeline._triage.classify_batch_async = AsyncMock(return_value=[])

        metadata = [
            {"msg_id": "known_1", "subject": "Old email", "from": "a@b.com", "snippet": "s", "date": "2025-01-01"},
            {"msg_id": "new_1", "subject": "New email", "from": "c@d.com", "snippet": "s", "date": "2025-01-01"},
        ]

        events = []
        async for event in pipeline.triage(metadata, skip_msg_ids={"known_1"}):
            events.append(event)

        # classify_batch_async should only receive the new email
        pipeline._triage.classify_batch_async.assert_awaited_once()
        classified_emails = pipeline._triage.classify_batch_async.call_args[0][0]
        assert len(classified_emails) == 1
        assert classified_emails[0]["msg_id"] == "new_1"

    @pytest.mark.asyncio
    async def test_classifies_new_emails(self):
        pipeline = _make_pipeline_service()
        pipeline._triage.classify_batch_async = AsyncMock(return_value=[
            (True, "groq:yes", 1.0),
            (False, "groq:no", 0.0),
        ])

        metadata = [
            {"msg_id": "m1", "subject": "Policy copy", "from": "a@b.com", "snippet": "s", "date": "2025-01-01"},
            {"msg_id": "m2", "subject": "Newsletter", "from": "c@d.com", "snippet": "s", "date": "2025-01-01"},
        ]

        events = []
        async for event in pipeline.triage(metadata):
            events.append(event)

        # Last event should be stage_complete with relevant_emails
        final = events[-1]
        assert final["type"] == "stage_complete"
        assert final["relevant"] == 1
        assert final["skipped"] == 1

    @pytest.mark.asyncio
    @patch("services.pipeline_service.db_service.db")
    async def test_saves_to_db_when_user_id_set(self, mock_db):
        mock_db._client.batch = AsyncMock()
        mock_db.execute = AsyncMock()
        pipeline = _make_pipeline_service()
        pipeline._triage.classify_batch_async = AsyncMock(return_value=[
            (True, "groq:yes", 1.0),
        ])

        metadata = [
            {"msg_id": "m1", "subject": "Policy", "from": "a@b.com", "snippet": "s", "date": "2025-01-01"},
        ]

        async for _ in pipeline.triage(metadata, user_id=42):
            pass
    
        # Verify that it attempted to save to DB via _client.batch or execute
        assert mock_db._client.batch.called or mock_db.execute.called

    @pytest.mark.asyncio
    async def test_all_cached_yields_stage_complete(self):
        pipeline = _make_pipeline_service()

        metadata = [
            {"msg_id": "c1", "subject": "Old", "from": "a@b.com", "snippet": "s", "date": "2025-01-01"},
        ]

        events = []
        async for event in pipeline.triage(metadata, skip_msg_ids={"c1"}):
            events.append(event)

        final = events[-1]
        assert final["type"] == "stage_complete"
        assert final["cached"] == 1
        assert final["relevant"] == 0


class TestGrokExtract:
    @pytest.mark.asyncio
    async def test_parses_json_response(self):
        pipeline = _make_pipeline_service()

        policy_json = {
            "policy_number": "ABC123",
            "type": "health",
            "provider": "Test Insurance",
        }
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(policy_json)
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        pipeline.client.chat.completions.create = AsyncMock(return_value=mock_response)

        doc = {
            "pdf_filename": "test.pdf",
            "email_subject": "Your policy",
            "pdf_text": "Policy details here...",
        }
        result = await pipeline._grok_extract(doc)

        assert result is not None
        assert result["policy_number"] == "ABC123"
        assert result["source_pdf"] == "test.pdf"
        assert result["source_email"] == "Your policy"

    @pytest.mark.asyncio
    async def test_handles_skip_response(self):
        pipeline = _make_pipeline_service()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({"skip": True})
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=10)
        pipeline.client.chat.completions.create = AsyncMock(return_value=mock_response)

        doc = {"pdf_filename": "junk.pdf", "email_subject": "Not a policy", "pdf_text": "Junk text"}
        result = await pipeline._grok_extract(doc)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_json_error(self):
        pipeline = _make_pipeline_service()

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Not valid JSON at all"
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=10)
        pipeline.client.chat.completions.create = AsyncMock(return_value=mock_response)

        doc = {"pdf_filename": "bad.pdf", "email_subject": "Bad", "pdf_text": "text"}
        result = await pipeline._grok_extract(doc)
        assert result is None

    @pytest.mark.asyncio
    async def test_adds_password_hint_for_locked_pdf(self):
        pipeline = _make_pipeline_service()

        policy_json = {"policy_number": "LOCK1", "type": "car", "provider": "ICICI Lombard"}
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(policy_json)
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        pipeline.client.chat.completions.create = AsyncMock(return_value=mock_response)

        doc = {
            "pdf_filename": "locked.pdf",
            "email_subject": "Your policy",
            "pdf_text": "[PASSWORD-PROTECTED PDF]",
            "_password_protected": True,
            "_locked_pdf_path": "/tmp/locked.pdf",
            "_password_hint": "Last four digits of vehicle registration",
        }
        result = await pipeline._grok_extract(doc)

        assert result is not None
        assert result["password_protected"] is True
        assert result["locked_pdf_path"] == "/tmp/locked.pdf"
        assert "registration" in result["password_hint"]

    @pytest.mark.asyncio
    async def test_locked_pdf_uses_fallback_hint(self):
        pipeline = _make_pipeline_service()

        policy_json = {"policy_number": "LOCK2", "type": "car", "provider": "HDFC ERGO"}
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(policy_json)
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        pipeline.client.chat.completions.create = AsyncMock(return_value=mock_response)

        doc = {
            "pdf_filename": "locked.pdf",
            "email_subject": "Your policy",
            "email_from": "noreply@hdfcergo.com",
            "pdf_text": "[PASSWORD-PROTECTED PDF]",
            "_password_protected": True,
            "_locked_pdf_path": "/tmp/locked.pdf",
            "_password_hint": "",  # no hint from email
        }
        result = await pipeline._grok_extract(doc)

        assert result is not None
        assert "DDMMYYYY" in result["password_hint"]
