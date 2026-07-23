"""Tests for services/db_service.py domain functions with mocked DB."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.db_service import (
    _hash_vault_key,
    derive_key,
    get_cached_extractions,
    get_or_create_user,
    get_processed_msg_ids,
    save_extraction_result,
    save_final_policies,
    save_triage_result,
    verify_vault_key,
)


@pytest.fixture
def mock_db():
    with patch("services.db_service.db") as mock:
        mock.query = AsyncMock(return_value=[])
        mock.query_one = AsyncMock(return_value=None)
        mock.execute = AsyncMock(return_value=MagicMock(last_insert_rowid=1))
        yield mock


class TestGetOrCreateUser:
    async def test_returns_existing_user_id(self, mock_db):
        mock_db.query_one = AsyncMock(return_value={"id": 42})
        user_id = await get_or_create_user("test@test.com", "Test")
        assert user_id == 42

    async def test_creates_new_user(self, mock_db):
        mock_db.query_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=MagicMock(last_insert_rowid=99))
        user_id = await get_or_create_user("new@test.com", "New User")
        assert user_id == 99
        # Verify INSERT was called
        mock_db.execute.assert_awaited_once()
        call_args = mock_db.execute.call_args
        assert "INSERT INTO users" in call_args[0][0]


class TestVerifyVaultKey:
    async def test_first_use_sets_hash(self, mock_db):
        mock_db.query_one = AsyncMock(return_value={
            "id": 1, "email": "test@test.com", "vault_salt": "test_salt",
            "vault_hash": None, "name": "Test",
        })
        key = await verify_vault_key(1, "my_vault_key")
        assert isinstance(key, bytes)
        assert len(key) == 32
        # Should have called UPDATE to set vault_hash
        mock_db.execute.assert_awaited_once()

    async def test_correct_key(self, mock_db):
        salt = "test_salt"
        correct_hash = _hash_vault_key("correct_key", salt)
        mock_db.query_one = AsyncMock(return_value={
            "id": 1, "email": "test@test.com", "vault_salt": salt,
            "vault_hash": correct_hash, "name": "Test",
        })
        key = await verify_vault_key(1, "correct_key")
        assert isinstance(key, bytes)
        assert len(key) == 32

    async def test_wrong_key_raises(self, mock_db):
        salt = "test_salt"
        correct_hash = _hash_vault_key("correct_key", salt)
        mock_db.query_one = AsyncMock(return_value={
            "id": 1, "email": "test@test.com", "vault_salt": salt,
            "vault_hash": correct_hash, "name": "Test",
        })
        with pytest.raises(ValueError, match="Wrong vault key"):
            await verify_vault_key(1, "wrong_key")

    async def test_user_not_found_raises(self, mock_db):
        mock_db.query_one = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="User not found"):
            await verify_vault_key(999, "any_key")


class TestGetProcessedMsgIds:
    async def test_returns_set(self, mock_db):
        mock_db.query = AsyncMock(return_value=[
            {"msg_id": "a"}, {"msg_id": "b"}, {"msg_id": "c"},
        ])
        result = await get_processed_msg_ids(1)
        assert result == {"a", "b", "c"}

    async def test_empty_result(self, mock_db):
        mock_db.query = AsyncMock(return_value=[])
        result = await get_processed_msg_ids(1)
        assert result == set()


class TestSaveTriageResult:
    async def test_calls_execute(self, mock_db):
        await save_triage_result("msg_123", 1, True, "similarity:0.5")
        mock_db.execute.assert_awaited_once()
        sql = mock_db.execute.call_args[0][0]
        assert "INSERT OR REPLACE INTO processed_emails" in sql


class TestSaveAndGetExtractions:
    async def test_save_then_get_roundtrip(self, mock_db):
        key = derive_key("vault", "salt")
        policy = {"policy_number": "RT123", "status": "ACTIVE", "premium": 5000}

        # Save extraction
        await save_extraction_result("msg_1", 1, policy, key)
        # Capture what was saved
        save_call = mock_db.execute.call_args
        encrypted_value = save_call[0][1][0]  # first arg of params list

        # Mock get to return what was saved
        mock_db.query = AsyncMock(return_value=[
            {"msg_id": "msg_1", "extraction_json": encrypted_value}
        ])
        results, failed = await get_cached_extractions(1, key)
        assert len(results) == 1
        assert len(failed) == 0
        assert results[0]["policy_number"] == "RT123"


class TestSaveFinalPolicies:
    async def test_deletes_then_inserts(self, mock_db):
        key = derive_key("vault", "salt")
        policies = [
            {"policy_number": "P1", "status": "ACTIVE"},
            {"policy_number": "P2", "status": "EXPIRED"},
        ]
        await save_final_policies(1, policies, key)

        # Should have 3 calls: 1 DELETE + 2 INSERTs
        assert mock_db.execute.await_count == 3
        first_call = mock_db.execute.call_args_list[0]
        assert "DELETE FROM policies" in first_call[0][0]

    async def test_empty_policies(self, mock_db):
        key = derive_key("vault", "salt")
        await save_final_policies(1, [], key)
        # Just the DELETE call
        assert mock_db.execute.await_count == 1
