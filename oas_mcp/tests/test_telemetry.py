"""Unit tests for telemetry and logging utilities."""

from __future__ import annotations

import pytest
import numpy as np
from oas_mcp.core.telemetry import get_run_logs, make_telemetry, redact


class TestRedact:
    def test_numpy_array_becomes_summary(self):
        arr = np.array([1.0, 2.0, 3.0])
        result = redact(arr)
        assert isinstance(result, dict)
        assert result["shape"] == [3]
        assert "hash" in result
        assert result["min"] == pytest.approx(1.0)
        assert result["max"] == pytest.approx(3.0)

    def test_dict_is_recursively_redacted(self):
        obj = {"mesh": np.ones((3, 3, 3)), "CL": 0.5}
        result = redact(obj)
        assert isinstance(result["mesh"], dict)
        assert result["CL"] == 0.5

    def test_long_list_is_truncated(self):
        obj = list(range(25))
        result = redact(obj)
        assert "25 items" in str(result)

    def test_short_list_passes_through(self):
        obj = [1, 2, 3]
        result = redact(obj)
        assert result == [1, 2, 3]

    def test_scalar_passes_through(self):
        assert redact(42) == 42
        assert redact("hello") == "hello"


class TestMakeTelemetry:
    def test_basic_telemetry(self):
        telem = make_telemetry(elapsed_s=0.123, cache_hit=True, surface_count=2)
        assert telem["elapsed_s"] == pytest.approx(0.123)
        assert telem["oas.cache.hit"] is True
        assert telem["oas.surface.count"] == 2

    def test_mesh_shape_included(self):
        telem = make_telemetry(0.1, False, 1, mesh_shape=(2, 7, 3))
        assert telem["oas.mesh.nx"] == 2
        assert telem["oas.mesh.ny"] == 7

    def test_extra_keys_included(self):
        telem = make_telemetry(0.1, False, extra={"custom": "value"})
        assert telem["custom"] == "value"

    def test_elapsed_rounded_to_4dp(self):
        telem = make_telemetry(0.123456789, False)
        assert telem["elapsed_s"] == pytest.approx(0.1235, abs=1e-4)


class TestRunLogs:
    def test_unknown_run_id_returns_empty(self):
        logs = get_run_logs("nonexistent_run_id_xyz")
        assert logs is None or logs == []

    def test_log_capture_during_span(self):
        import logging
        from oas_mcp.core.telemetry import span, logger
        import asyncio

        run_id = "test_run_" + "abc123"

        async def _run():
            async with asyncio.timeout(5):
                with span("test_tool", run_id, "default") as attrs:
                    logger.info("test message from span")
                    attrs["test_key"] = "test_value"

        asyncio.get_event_loop().run_until_complete(_run())
        logs = get_run_logs(run_id)
        assert logs is not None
        messages = [r["message"] for r in logs]
        # Should have at least START and END records
        assert any("test_tool" in m for m in messages)
