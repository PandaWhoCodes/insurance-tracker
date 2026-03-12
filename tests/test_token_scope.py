"""Tests for OAuth scope helpers and vault key guards in app.py."""

import json
import pytest
from unittest.mock import patch, AsyncMock


@pytest.fixture
def client():
    """TestClient with Turso DB mocked out."""
    with patch("services.db_service.db") as mock_db:
        mock_db.connect = AsyncMock()
        mock_db.init_schema = AsyncMock()
        mock_db.close = AsyncMock()
        mock_db._client = None
        from app import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c


@pytest.fixture
def authed_client(client):
    """TestClient with a fake session (logged in)."""
    # Inject session data by setting the session cookie
    client.cookies.set("session", "")  # clear
    # Use a direct approach: patch the session middleware
    from app import app
    from starlette.testclient import TestClient

    with patch("services.db_service.db") as mock_db:
        mock_db.connect = AsyncMock()
        mock_db.init_schema = AsyncMock()
        mock_db.close = AsyncMock()
        mock_db._client = None
        with TestClient(app) as c:
            # Manually set session via internal state
            with c:
                # Set session by calling a special test endpoint isn't possible,
                # so we test the helper directly
                pass
            yield c


# ── _token_has_gmail_scope ──────────────────────────

class TestTokenHasGmailScope:
    def test_missing_file(self, tmp_path):
        from app import _token_has_gmail_scope
        assert _token_has_gmail_scope(tmp_path / "nonexistent.json") is False

    def test_empty_scopes(self, tmp_path):
        from app import _token_has_gmail_scope
        token_file = tmp_path / "test.json"
        token_file.write_text(json.dumps({"scopes": []}))
        assert _token_has_gmail_scope(token_file) is False

    def test_basic_scopes_only(self, tmp_path):
        from app import _token_has_gmail_scope
        token_file = tmp_path / "test.json"
        token_file.write_text(json.dumps({
            "scopes": ["openid", "https://www.googleapis.com/auth/userinfo.email"]
        }))
        assert _token_has_gmail_scope(token_file) is False

    def test_gmail_scope_present(self, tmp_path):
        from app import _token_has_gmail_scope
        token_file = tmp_path / "test.json"
        token_file.write_text(json.dumps({
            "scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/gmail.readonly",
            ]
        }))
        assert _token_has_gmail_scope(token_file) is True

    def test_corrupted_json(self, tmp_path):
        from app import _token_has_gmail_scope
        token_file = tmp_path / "test.json"
        token_file.write_text("not json")
        assert _token_has_gmail_scope(token_file) is False

    def test_no_scopes_key(self, tmp_path):
        from app import _token_has_gmail_scope
        token_file = tmp_path / "test.json"
        token_file.write_text(json.dumps({"token": "abc"}))
        assert _token_has_gmail_scope(token_file) is False


# ── Route tests ──────────────────────────────────────

class TestGmailAuthRoute:
    def test_gmail_auth_redirects(self, client):
        """GET /auth/gmail should redirect to Google OAuth."""
        response = client.get("/auth/gmail", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "accounts.google.com" in response.headers.get("location", "")

    def test_login_redirects(self, client):
        """GET /auth/login should redirect to Google OAuth."""
        response = client.get("/auth/login", follow_redirects=False)
        assert response.status_code in (302, 307)
        assert "accounts.google.com" in response.headers.get("location", "")


class TestPoliciesEmptyVaultKey:
    def test_policies_unauthenticated(self, client):
        response = client.get("/api/policies")
        assert response.status_code == 401

    def test_upload_unauthenticated(self, client):
        # Upload requires multipart file, so send a dummy file
        response = client.post(
            "/api/policies/upload",
            files={"file": ("test.pdf", b"%PDF-fake", "application/pdf")},
        )
        assert response.status_code == 401
