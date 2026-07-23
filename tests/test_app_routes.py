"""Tests for FastAPI routes in app.py."""

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
        # Import app after patching
        from app import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c


class TestIndexRoute:
    def test_returns_html(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_how_it_works(self, client):
        response = client.get("/how-it-works")
        assert response.status_code == 200

    def test_privacy(self, client):
        response = client.get("/privacy")
        assert response.status_code == 200

    def test_terms(self, client):
        response = client.get("/terms")
        assert response.status_code == 200


class TestApiMe:
    def test_unauthenticated(self, client):
        response = client.get("/api/me")
        assert response.status_code == 401
        assert response.json()["authenticated"] is False


class TestApiPolicies:
    def test_unauthenticated(self, client):
        response = client.post("/api/policies", json={"vault_key": ""})
        assert response.status_code == 401

    def test_refresh_unauthenticated(self, client):
        response = client.post("/api/policies/refresh")
        assert response.status_code == 401

    def test_refresh_stream_unauthenticated(self, client):
        response = client.get("/api/policies/refresh-stream")
        assert response.status_code == 401
