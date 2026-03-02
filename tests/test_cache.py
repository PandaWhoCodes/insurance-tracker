"""Tests for services/cache_service.py."""

import json
from datetime import datetime, timedelta
import pytest
from freezegun import freeze_time

from services.cache_service import CacheService


@pytest.fixture
def cache(tmp_path):
    """CacheService using a temp directory."""
    svc = CacheService()
    svc.cache_dir = tmp_path
    return svc


class TestCachePath:
    def test_sanitizes_email(self, cache):
        path = cache._cache_path("user@example.com")
        assert "@" not in path.name
        assert "." not in path.stem  # dots replaced with _
        assert path.name == "user_at_example_com.json"


class TestCacheGetSet:
    @freeze_time("2026-03-03")
    def test_set_then_get(self, cache):
        policies = [{"policy_number": "123", "status": "ACTIVE"}]
        cache.set("user@test.com", policies)
        result = cache.get("user@test.com")
        assert result is not None
        assert result["policies"] == policies
        assert result["user_email"] == "user@test.com"

    def test_get_nonexistent(self, cache):
        assert cache.get("noone@test.com") is None

    @freeze_time("2026-03-03")
    def test_get_expired_returns_none(self, cache):
        # Write a cache file with old fetched_at
        path = cache._cache_path("old@test.com")
        old_date = (datetime.now() - timedelta(days=60)).isoformat()
        data = {"user_email": "old@test.com", "fetched_at": old_date, "policies": []}
        with open(path, "w") as f:
            json.dump(data, f)
        assert cache.get("old@test.com") is None

    @freeze_time("2026-03-03")
    def test_stores_correct_fields(self, cache):
        cache.set("test@test.com", [{"p": 1}])
        path = cache._cache_path("test@test.com")
        with open(path) as f:
            data = json.load(f)
        assert "user_email" in data
        assert "fetched_at" in data
        assert "policies" in data


class TestCacheInvalidate:
    @freeze_time("2026-03-03")
    def test_invalidate_removes_file(self, cache):
        cache.set("del@test.com", [])
        path = cache._cache_path("del@test.com")
        assert path.exists()
        cache.invalidate("del@test.com")
        assert not path.exists()

    def test_invalidate_nonexistent_is_safe(self, cache):
        cache.invalidate("nofile@test.com")  # should not raise
