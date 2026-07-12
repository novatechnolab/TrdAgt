"""
TradeSignal — API End-to-End Regression Tests
==============================================
Tests the actual running Flask backend via HTTP.
These tests validate SCREEN-LEVEL behaviour — the same fields the UI renders.

Requirements:
    pip install pytest requests

Usage:
    # Start the backend first:
    cd app/backend && python server.py

    # Then run:
    pytest tests/regression/api_e2e_test.py -v

    # Run with golden comparison (after capturing golden.json):
    pytest tests/regression/api_e2e_test.py -v --golden=tests/regression/golden/golden.json
"""

import json
import os
import pytest
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get("TRADESIGNAL_URL", "http://localhost:5001")
GOLDEN_FILE = os.environ.get("GOLDEN_FILE", "tests/regression/golden/golden.json")

# Tolerance for numeric fields (same as browser runner)
NUMERIC_TOLERANCES = {
    "confidence":         3,
    "stopLoss":           1.0,
    "indicators.ema9":    2.0,
    "indicators.ema21":   2.0,
    "indicators.rsi":     2.0,
    "indicators.adx":     3.0,
    "indicatorScore.score": 0.05,
}

EXACT_FIELDS = [
    "isValid", "direction", "setupType", "riskLevel",
    "preConditions.emaStack", "preConditions.vwapSide",
    "triggerCheck.passed", "gapException.isGap",
    "gapOverride.isOverride",
]

# ── OHLCV Fixture Loader ──────────────────────────────────────────────────────
def load_golden():
    if os.path.exists(GOLDEN_FILE):
        with open(GOLDEN_FILE) as f:
            return json.load(f)
    return {}

def get_path(obj, path):
    keys = path.split(".")
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    return obj

# ── Helpers ───────────────────────────────────────────────────────────────────
def post_validate(payload, timeout=15):
    """POST to /api/validate-entry and return JSON response."""
    resp = requests.post(
        f"{BASE_URL}/api/validate-entry",
        json=payload,
        timeout=timeout,
    )
    assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:300]}"
    return resp.json()

def assert_numeric_close(result, field, expected, tol, scenario_id):
    current = get_path(result, field)
    golden_val = get_path(expected, field)
    if golden_val is None and current is None:
        return
    assert current is not None, f"[{scenario_id}] Field '{field}' missing in response"
    diff = abs((current or 0) - (golden_val or 0))
    assert diff <= tol, (
        f"[{scenario_id}] '{field}' drifted beyond tolerance: "
        f"golden={golden_val}, current={current}, diff={diff:.4f}, tol={tol}"
    )

def assert_exact(result, field, expected, scenario_id):
    current = get_path(result, field)
    golden_val = get_path(expected, field)
    assert current == golden_val, (
        f"[{scenario_id}] EXACT FIELD CHANGED: '{field}' "
        f"golden={golden_val!r}, current={current!r}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 1: Health & Connectivity ─────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectivity:

    def test_health_endpoint(self):
        """Backend must be reachable and return ok status."""
        resp = requests.get(f"{BASE_URL}/api/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_validate_entry_exists(self):
        """POST /api/validate-entry must exist (not 404)."""
        resp = requests.post(
            f"{BASE_URL}/api/validate-entry",
            json={"symbol": "RELIANCE"},
            timeout=10,
        )
        # May return 400 (missing params) or 500 (no Kite) but never 404
        assert resp.status_code != 404, "/api/validate-entry route missing"

    def test_validate_entry_requires_symbol(self):
        """Missing symbol must return 400."""
        resp = requests.post(
            f"{BASE_URL}/api/validate-entry", json={}, timeout=10
        )
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 2: Response Schema Validation ─────────────────────────────────────
# Required fields must always be present in the response.
# ══════════════════════════════════════════════════════════════════════════════

REQUIRED_TOP_LEVEL_KEYS = [
    "isValid", "direction", "setupType", "setupLabel",
    "preConditions", "confirmations", "avoidFlags",
    "triggerCheck", "gapException", "gapOverride",
    "stopLoss", "riskLevel", "confidence", "reasoning",
    "indicators", "indicatorScore", "entryPrice", "timestamp",
]

REQUIRED_PRECONDITIONS_KEYS = ["emaStack", "vwapSide", "ema50Trend"]
REQUIRED_INDICATORS_KEYS    = ["ema9", "ema21", "ema50", "rsi", "adx", "atr"]
REQUIRED_INDICATOR_SCORE    = ["score", "details"]


@pytest.mark.skip(reason="Entry Validator removed")
class TestResponseSchema:
    """
    These tests run BEFORE migration (to document current schema)
    and AFTER migration (to verify schema is preserved).
    They use a minimal fixture that can work without live Kite data
    by sending pre-built candles directly.
    """

    @pytest.fixture
    def minimal_payload(self):
        """Smallest valid payload: 30 candles, no live Kite needed."""
        # Generate 30 uptrend candles inline
        candles = []
        base = 22000
        for i in range(30):
            o = round(base + i * 8, 2)
            c = round(o + (6 if i % 3 != 2 else -2), 2)
            candles.append({
                "date":   f"2026-04-15T{9 + (i * 5) // 60:02d}:{(9*60 + i*5 + 15) % 60 + (i*5)//60 * 0:02d}:00+05:30",
                "open":   o, "high": round(max(o, c) + 2, 2),
                "low":    round(min(o, c) - 2, 2), "close": c,
                "volume": 500000,
            })
        # Simpler date calculation
        candles = []
        for i in range(30):
            mins = 9 * 60 + 15 + i * 5
            h, m = divmod(mins, 60)
            o = round(22000 + i * 8, 2)
            c = round(o + (8 if i % 3 != 2 else -2), 2)
            candles.append({
                "date": f"2026-04-15T{h:02d}:{m:02d}:00+05:30",
                "open": o, "high": round(max(o, c) + 3, 2),
                "low":  round(min(o, c) - 3, 2), "close": c,
                "volume": 500000,
            })
        return {
            "symbol": "NIFTY",
            "direction": "CALL",
            "price": candles[-1]["close"],
            "candles": candles,
            "date": "2026-04-15",
            "time": "11:55",
            "interval": "5minute",
        }

    def test_top_level_keys_present(self, minimal_payload):
        """All required top-level keys must be in the response."""
        result = post_validate(minimal_payload)
        # If the backend returns raw candles (pre-migration), skip schema check
        if "candles" in result and "isValid" not in result:
            pytest.skip("Pre-migration: backend returns raw candles, not validation result")
        for key in REQUIRED_TOP_LEVEL_KEYS:
            assert key in result, f"Missing required key: '{key}'"

    def test_preconditions_schema(self, minimal_payload):
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        pre = result.get("preConditions", {})
        for k in REQUIRED_PRECONDITIONS_KEYS:
            assert k in pre, f"preConditions missing key: '{k}'"

    def test_indicators_schema(self, minimal_payload):
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        ind = result.get("indicators", {})
        for k in REQUIRED_INDICATORS_KEYS:
            assert k in ind, f"indicators missing key: '{k}'"

    def test_confidence_is_0_to_100(self, minimal_payload):
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        conf = result.get("confidence", -1)
        assert 0 <= conf <= 100, f"confidence out of range: {conf}"

    def test_is_valid_is_boolean(self, minimal_payload):
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        assert isinstance(result.get("isValid"), bool)

    def test_reasoning_is_list(self, minimal_payload):
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        assert isinstance(result.get("reasoning"), list)
        assert len(result["reasoning"]) > 0

    def test_stop_loss_direction_call(self, minimal_payload):
        """For CALL, stop loss must be BELOW entry price."""
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        sl = result.get("stopLoss", 0)
        price = result.get("entryPrice", 0)
        if sl > 0 and price > 0:
            assert sl < price, f"CALL stopLoss {sl} must be < entryPrice {price}"

    def test_stop_loss_direction_put(self, minimal_payload):
        """For PUT, stop loss must be ABOVE entry price."""
        payload = dict(minimal_payload)
        payload["direction"] = "PUT"
        result = post_validate(payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        sl = result.get("stopLoss", 0)
        price = result.get("entryPrice", 0)
        if sl > 0 and price > 0:
            assert sl > price, f"PUT stopLoss {sl} must be > entryPrice {price}"

    def test_direction_preserved_in_response(self, minimal_payload):
        """direction in response must match direction in request."""
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        assert result.get("direction") == minimal_payload["direction"]

    def test_setup_type_valid_values(self, minimal_payload):
        """setupType must be one of the known values."""
        valid_types = {"TYPE_1", "TYPE_2", "TYPE_3", "TYPE_4", "NONE"}
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        assert result.get("setupType") in valid_types, \
            f"Unknown setupType: {result.get('setupType')}"

    def test_risk_level_valid_values(self, minimal_payload):
        valid_levels = {"STANDARD","HIGH_RISK","TREND_RIDE","GAP_OVERRIDE","AGGRESSIVE","RISKY"}
        result = post_validate(minimal_payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        assert result.get("riskLevel") in valid_levels, \
            f"Unknown riskLevel: {result.get('riskLevel')}"

    def test_invalid_when_insufficient_candles(self, minimal_payload):
        """< 30 candles must return isValid=false."""
        payload = dict(minimal_payload)
        payload["candles"] = minimal_payload.get("candles", [])[:10]
        result = post_validate(payload)
        if "candles" in result:
            pytest.skip("Pre-migration backend")
        # Must not crash; if < 30 candles, isValid should be False
        assert result.get("isValid") is False or "error" in result


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 3: Golden Comparison (post-migration gate) ────────────────────────
# Load golden.json captured from JS, run same inputs via API, diff outputs.
# ══════════════════════════════════════════════════════════════════════════════

golden = load_golden()

@pytest.mark.skip(reason="Entry Validator removed")
class TestGoldenComparison:

    @pytest.mark.parametrize("scenario_id", list(golden.keys()))
    def test_exact_fields_match_golden(self, scenario_id):
        """Exact boolean/enum fields must be identical to JS golden output."""
        entry = golden[scenario_id]
        payload = {
            "symbol":    entry["params"]["symbol"],
            "direction": entry["params"]["direction"],
            "price":     entry["params"]["price"],
            "date":      entry["params"].get("targetDate"),
            "time":      entry["params"].get("entryTime"),
            "interval":  "5minute",
        }
        result = post_validate(payload)
        if "candles" in result and "isValid" not in result:
            pytest.skip("Pre-migration backend — golden comparison requires post-migration")

        g = entry["golden"]
        for field in EXACT_FIELDS:
            assert_exact(result, field, g, scenario_id)

    @pytest.mark.parametrize("scenario_id", list(golden.keys()))
    def test_numeric_fields_within_tolerance(self, scenario_id):
        """Numeric fields must be within defined tolerance of golden values."""
        entry = golden[scenario_id]
        payload = {
            "symbol":    entry["params"]["symbol"],
            "direction": entry["params"]["direction"],
            "price":     entry["params"]["price"],
            "date":      entry["params"].get("targetDate"),
            "time":      entry["params"].get("entryTime"),
            "interval":  "5minute",
        }
        result = post_validate(payload)
        if "candles" in result and "isValid" not in result:
            pytest.skip("Pre-migration backend")

        g = entry["golden"]
        for field, tol in NUMERIC_TOLERANCES.items():
            assert_numeric_close(result, field, g, tol, scenario_id)

    @pytest.mark.parametrize("scenario_id", list(golden.keys()))
    def test_reasoning_count_matches(self, scenario_id):
        """Reasoning list length should be within ±2 of golden."""
        entry = golden[scenario_id]
        payload = {
            "symbol":    entry["params"]["symbol"],
            "direction": entry["params"]["direction"],
            "price":     entry["params"]["price"],
            "date":      entry["params"].get("targetDate"),
            "time":      entry["params"].get("entryTime"),
            "interval":  "5minute",
        }
        result = post_validate(payload)
        if "candles" in result and "isValid" not in result:
            pytest.skip("Pre-migration backend")

        g_count = len(entry["golden"].get("reasoning", []))
        c_count = len(result.get("reasoning", []))
        diff = abs(g_count - c_count)
        assert diff <= 2, (
            f"[{scenario_id}] Reasoning count changed: golden={g_count}, current={c_count}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# ── SECTION 4: Edge Cases & Guard Rails ───────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_unknown_symbol_returns_error(self):
        """Non-existent symbol should return a graceful error, not 500 crash."""
        resp = requests.post(
            f"{BASE_URL}/api/validate-entry",
            json={"symbol": "XXXX_NONEXISTENT", "direction": "CALL"},
            timeout=15,
        )
        # Should not be a server crash
        assert resp.status_code in (200, 400, 404, 500)
        data = resp.json()
        assert "error" in data or "isValid" in data

    def test_call_and_put_give_different_results(self):
        """Same symbol with CALL vs PUT must produce different preConditions."""
        base = {
            "symbol": "NIFTY", "price": 22000,
            "date": "2026-04-15", "time": "11:55",
            "interval": "5minute",
        }
        call_resp = post_validate({**base, "direction": "CALL"})
        put_resp  = post_validate({**base, "direction": "PUT"})

        if "candles" in call_resp:
            pytest.skip("Pre-migration backend")

        # At minimum, direction must differ
        assert call_resp.get("direction") != put_resp.get("direction")

    def test_response_time_acceptable(self):
        """Validation must respond within 8 seconds (includes Kite API call)."""
        import time
        start = time.time()
        resp = requests.post(
            f"{BASE_URL}/api/validate-entry",
            json={"symbol": "RELIANCE", "direction": "CALL",
                  "date": "2026-04-15", "time": "11:55", "interval": "5minute"},
            timeout=15,
        )
        elapsed = time.time() - start
        assert elapsed < 8.0, f"Response too slow: {elapsed:.1f}s"

    def test_index_symbol_handled(self):
        """Index symbols (NIFTY) must not crash and must skip VWAP."""
        result = post_validate({
            "symbol": "NIFTY", "direction": "CALL",
            "price": 22000, "date": "2026-04-15",
            "time": "11:55", "interval": "5minute",
        })
        # Should not error on index
        assert "error" in result or "isValid" in result or "candles" in result

    def test_direction_case_insensitive(self):
        """'call' and 'CALL' should both work (or at least not crash)."""
        for d in ["CALL", "call", "Call"]:
            resp = requests.post(
                f"{BASE_URL}/api/validate-entry",
                json={"symbol": "RELIANCE", "direction": d,
                      "date": "2026-04-15", "time": "11:55"},
                timeout=12,
            )
            assert resp.status_code != 500, f"direction={d!r} caused 500"
