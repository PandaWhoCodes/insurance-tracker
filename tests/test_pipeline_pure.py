"""Tests for pure functions in services/pipeline_service.py."""

import pytest
from unittest.mock import patch
from freezegun import freeze_time

from services.pipeline_service import _strip_json, _get_password_hint, LIC_PLAN_NAMES
from tests.conftest import _make_policy


def _make_pipeline_service():
    """Create PipelineService with external deps patched out."""
    with patch("services.pipeline_service.TriageService"):
        with patch("services.pipeline_service.AsyncOpenAI"):
            from services.pipeline_service import PipelineService
            return PipelineService()


# ── _strip_json ──────────────────────────────────────


class TestStripJson:
    def test_bare_json(self):
        assert _strip_json('{"key": "value"}') == '{"key": "value"}'

    def test_markdown_code_fence_with_lang(self):
        content = '```json\n{"key": "value"}\n```'
        assert _strip_json(content) == '{"key": "value"}'

    def test_markdown_code_fence_without_lang(self):
        content = '```\n{"key": "value"}\n```'
        assert _strip_json(content) == '{"key": "value"}'

    def test_trailing_fence_only(self):
        content = '{"key": "value"}\n```'
        assert _strip_json(content) == '{"key": "value"}'

    def test_no_fences(self):
        content = "plain text"
        assert _strip_json(content) == "plain text"


# ── _get_password_hint ───────────────────────────────


class TestGetPasswordHint:
    def test_hdfc_ergo(self):
        hint = _get_password_hint("noreply@hdfcergo.com", "HDFC ERGO")
        assert "DDMMYYYY" in hint

    def test_lic(self):
        hint = _get_password_hint("support@lic.com", "LIC of India")
        assert "DDMMYYYY" in hint

    def test_icici_lombard(self):
        hint = _get_password_hint("noreply@icicilombard.com", "ICICI Lombard")
        assert "DDMMYYYY" in hint

    def test_case_insensitive(self):
        hint = _get_password_hint("NOREPLY@HDFCERGO.COM", "hdfc ergo")
        assert "DDMMYYYY" in hint

    def test_unknown_provider(self):
        hint = _get_password_hint("unknown@example.com", "Unknown Insurance")
        assert "date of birth" in hint.lower() or "PAN" in hint

    def test_none_inputs(self):
        hint = _get_password_hint(None, None)
        assert isinstance(hint, str)
        assert len(hint) > 0


# ── _local_dedup ─────────────────────────────────────


class TestLocalDedup:
    @freeze_time("2026-03-03")
    def test_empty_input(self):
        pipeline = _make_pipeline_service()
        assert pipeline._local_dedup([]) == []

    @freeze_time("2026-03-03")
    def test_single_policy_passthrough(self):
        pipeline = _make_pipeline_service()
        p = _make_policy(policy_end="2027-01-01")
        result = pipeline._local_dedup([p])
        assert len(result) == 1
        assert result[0]["status"] == "ACTIVE"

    @freeze_time("2026-03-03")
    def test_fixes_status_active(self):
        pipeline = _make_pipeline_service()
        p = _make_policy(policy_end="2027-01-01", status="EXPIRED")
        result = pipeline._local_dedup([p])
        assert result[0]["status"] == "ACTIVE"  # future end → ACTIVE

    @freeze_time("2026-03-03")
    def test_fixes_status_expired(self):
        pipeline = _make_pipeline_service()
        p = _make_policy(policy_end="2025-01-01", status="ACTIVE")
        result = pipeline._local_dedup([p])
        assert result[0]["status"] == "EXPIRED"  # past end → EXPIRED

    @freeze_time("2026-03-03")
    def test_hdfc_renewal_chain_dedup(self):
        """HDFC-style long numeric policy numbers with renewal suffixes merge."""
        pipeline = _make_pipeline_service()
        p1 = _make_policy(
            policy_number="2805205442589301000",
            policy_end="2025-05-22",
            status="EXPIRED",
            sum_insured=None,
        )
        p2 = _make_policy(
            policy_number="2805205442589302000",
            policy_end="2027-05-22",
            status="ACTIVE",
            sum_insured=1500000,
        )
        result = pipeline._local_dedup([p1, p2])
        assert len(result) == 1
        assert result[0]["status"] == "ACTIVE"
        assert result[0]["sum_insured"] == 1500000

    @freeze_time("2026-03-03")
    def test_acko_suffix_dedup(self, car_policy_pair):
        """Acko /01 and /02 suffixes are grouped together."""
        pipeline = _make_pipeline_service()
        p1, p2 = car_policy_pair
        result = pipeline._local_dedup([p1, p2])
        assert len(result) == 1
        assert result[0]["status"] == "ACTIVE"
        assert result[0]["nominee"] is not None  # merged from p2

    @freeze_time("2026-03-03")
    def test_prefers_active_over_expired(self):
        pipeline = _make_pipeline_service()
        expired = _make_policy(
            policy_number="SAME123",
            policy_end="2025-01-01",
            status="EXPIRED",
            sum_insured=500000,
            nominee={"name": "Nominee", "relationship": "Spouse"},
        )
        active = _make_policy(
            policy_number="SAME123",
            policy_end="2027-01-01",
            status="ACTIVE",
            sum_insured=700000,
            nominee=None,
        )
        result = pipeline._local_dedup([expired, active])
        assert len(result) == 1
        assert result[0]["status"] == "ACTIVE"
        assert result[0]["sum_insured"] == 700000
        # nominee merged from expired
        assert result[0]["nominee"] is not None

    @freeze_time("2026-03-03")
    def test_merges_complementary_fields(self):
        pipeline = _make_pipeline_service()
        p1 = _make_policy(
            policy_number="MERGE123",
            policy_end="2027-01-01",
            nominee=None,
            coverages=["Own Damage"],
        )
        p2 = _make_policy(
            policy_number="MERGE123",
            policy_end="2027-01-01",
            nominee={"name": "Test", "relationship": "Spouse"},
            coverages=None,
        )
        result = pipeline._local_dedup([p1, p2])
        assert len(result) == 1
        assert result[0]["coverages"] == ["Own Damage"]
        assert result[0]["nominee"] is not None

    @freeze_time("2026-03-03")
    def test_prefers_later_end_date(self):
        pipeline = _make_pipeline_service()
        p_early = _make_policy(
            policy_number="DATE123",
            policy_end="2027-01-01",
        )
        p_late = _make_policy(
            policy_number="DATE123",
            policy_end="2028-01-01",
        )
        result = pipeline._local_dedup([p_early, p_late])
        assert len(result) == 1
        assert result[0]["policy_end"] == "2028-01-01"

    @freeze_time("2026-03-03")
    def test_prefers_fewer_nulls(self):
        pipeline = _make_pipeline_service()
        sparse = _make_policy(
            policy_number="NULL123",
            policy_end="2027-01-01",
            sum_insured=None,
            premium=None,
            nominee=None,
        )
        full = _make_policy(
            policy_number="NULL123",
            policy_end="2027-01-01",
            sum_insured=500000,
            premium=10000,
            nominee={"name": "Test", "relationship": "Spouse"},
        )
        result = pipeline._local_dedup([sparse, full])
        assert len(result) == 1
        assert result[0]["sum_insured"] == 500000
        assert result[0]["premium"] == 10000

    @freeze_time("2026-03-03")
    def test_no_policy_number_kept_separately(self):
        pipeline = _make_pipeline_service()
        p1 = _make_policy(policy_number=None, provider="Provider A")
        p2 = _make_policy(policy_number=None, provider="Provider B")
        result = pipeline._local_dedup([p1, p2])
        assert len(result) == 2

    @freeze_time("2026-03-03")
    def test_lic_plan_name_enrichment(self):
        pipeline = _make_pipeline_service()
        p = _make_policy(
            provider="LIC of India",
            plan_name="945",
            policy_end="2050-01-01",
        )
        result = pipeline._local_dedup([p])
        assert result[0]["plan_name"] == "Jeevan Amar (Term Life)"

    @freeze_time("2026-03-03")
    def test_lic_enrichment_only_for_lic_provider(self):
        pipeline = _make_pipeline_service()
        p = _make_policy(
            provider="HDFC ERGO",
            plan_name="945",
            policy_end="2027-01-01",
        )
        result = pipeline._local_dedup([p])
        assert result[0]["plan_name"] == "945"  # NOT enriched

    @freeze_time("2026-03-03")
    def test_password_protected_flag_removed_on_merge(self):
        pipeline = _make_pipeline_service()
        locked = _make_policy(
            policy_number="LOCK456",
            policy_end="2027-01-01",
            password_protected=True,
            sum_insured=None,
        )
        unlocked = _make_policy(
            policy_number="LOCK456",
            policy_end="2027-01-01",
            sum_insured=500000,
        )
        result = pipeline._local_dedup([locked, unlocked])
        assert len(result) == 1
        assert "password_protected" not in result[0]
        assert result[0]["sum_insured"] == 500000

    @freeze_time("2026-03-03")
    def test_prefers_more_detailed_members(self):
        pipeline = _make_pipeline_service()
        sparse_members = _make_policy(
            policy_number="MEM123",
            policy_end="2027-01-01",
            insured_members=[{"name": "Test", "relationship": None, "date_of_birth": None}],
        )
        detailed_members = _make_policy(
            policy_number="MEM123",
            policy_end="2027-01-01",
            insured_members=[{"name": "Test User", "relationship": "Self", "date_of_birth": "1990-01-01"}],
        )
        result = pipeline._local_dedup([sparse_members, detailed_members])
        assert len(result) == 1
        assert result[0]["insured_members"][0]["date_of_birth"] == "1990-01-01"


# ── finalize ─────────────────────────────────────────


class TestFinalize:
    @freeze_time("2026-03-03")
    async def test_merges_existing_and_new(self):
        pipeline = _make_pipeline_service()
        existing = [_make_policy(policy_number="EX1", policy_end="2027-01-01")]
        new = [_make_policy(policy_number="NEW1", policy_end="2028-01-01")]
        result = await pipeline.finalize(new, existing_policies=existing)
        assert len(result) == 2
        numbers = {p["policy_number"] for p in result}
        assert "EX1" in numbers
        assert "NEW1" in numbers

    @freeze_time("2026-03-03")
    async def test_deduplicates_across_existing_and_new(self):
        pipeline = _make_pipeline_service()
        existing = [_make_policy(policy_number="SAME1", policy_end="2027-01-01", sum_insured=None)]
        new = [_make_policy(policy_number="SAME1", policy_end="2027-01-01", sum_insured=500000)]
        result = await pipeline.finalize(new, existing_policies=existing)
        assert len(result) == 1
        assert result[0]["sum_insured"] == 500000

    @freeze_time("2026-03-03")
    async def test_empty_both(self):
        pipeline = _make_pipeline_service()
        result = await pipeline.finalize([], existing_policies=[])
        assert result == []

    @freeze_time("2026-03-03")
    async def test_none_existing(self):
        pipeline = _make_pipeline_service()
        result = await pipeline.finalize([_make_policy(policy_number="A1", policy_end="2027-01-01")])
        assert len(result) == 1


# ── LIC_PLAN_NAMES ───────────────────────────────────


class TestLicPlanNames:
    def test_known_plan_numbers(self):
        assert "945" in LIC_PLAN_NAMES
        assert LIC_PLAN_NAMES["945"] == "Jeevan Amar (Term Life)"
        assert "814" in LIC_PLAN_NAMES

    def test_plan_names_are_descriptive(self):
        for number, name in LIC_PLAN_NAMES.items():
            assert len(name) > 3, f"Plan {number} has too short a name: {name}"
