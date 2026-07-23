import sys
import os
import pytest

# Ensure app/backend is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from oi_spurt_routes import compute_atm_5_analysis

def test_compute_atm_5_analysis_basic():
    # Mock chain sorted with irregular steps (like TVSMOTOR: 3840, 3850, 3860, 3880, 3900, 3920, 3940, 3950, 3960, 4000)
    mock_chain = [
        {"strike": 3840, "ce_oi": 1000, "ce_oi_chg": 100, "ce_ltp": 90, "ce_prev_ltp": 80, "pe_oi": 500, "pe_oi_chg": 50, "pe_ltp": 20, "pe_prev_ltp": 25},
        {"strike": 3850, "ce_oi": 1200, "ce_oi_chg": -200, "ce_ltp": 80, "ce_prev_ltp": 70, "pe_oi": 800, "pe_oi_chg": 100, "pe_ltp": 25, "pe_prev_ltp": 30},
        {"strike": 3860, "ce_oi": 1500, "ce_oi_chg": -300, "ce_ltp": 70, "ce_prev_ltp": 60, "pe_oi": 1000, "pe_oi_chg": 200, "pe_ltp": 30, "pe_prev_ltp": 35},
        {"strike": 3880, "ce_oi": 2000, "ce_oi_chg": -500, "ce_ltp": 60, "ce_prev_ltp": 50, "pe_oi": 1500, "pe_oi_chg": 300, "pe_ltp": 35, "pe_prev_ltp": 40},
        {"strike": 3900, "ce_oi": 5000, "ce_oi_chg": -1000, "ce_ltp": 50, "ce_prev_ltp": 40, "pe_oi": 6000, "pe_oi_chg": 1500, "pe_ltp": 40, "pe_prev_ltp": 45}, # ATM PE heavy
        {"strike": 3920, "ce_oi": 3000, "ce_oi_chg": -800, "ce_ltp": 40, "ce_prev_ltp": 30, "pe_oi": 2000, "pe_oi_chg": 500, "pe_ltp": 50, "pe_prev_ltp": 55}, # ATM CE
        {"strike": 3940, "ce_oi": 4000, "ce_oi_chg": -900, "ce_ltp": 30, "ce_prev_ltp": 20, "pe_oi": 1000, "pe_oi_chg": 200, "pe_ltp": 60, "pe_prev_ltp": 65},
        {"strike": 3950, "ce_oi": 2500, "ce_oi_chg": -400, "ce_ltp": 25, "ce_prev_ltp": 15, "pe_oi": 500, "pe_oi_chg": 100, "pe_ltp": 70, "pe_prev_ltp": 75},
        {"strike": 3960, "ce_oi": 1800, "ce_oi_chg": -200, "ce_ltp": 20, "ce_prev_ltp": 10, "pe_oi": 300, "pe_oi_chg": 50, "pe_ltp": 80, "pe_prev_ltp": 85},
        {"strike": 4000, "ce_oi": 6000, "ce_oi_chg": 500, "ce_ltp": 10, "ce_prev_ltp": 15, "pe_oi": 100, "pe_oi_chg": 10, "pe_ltp": 100, "pe_prev_ltp": 90},
    ]
    ltp = 3907.60
    atm_idx = 4 # 3900 strike

    result = compute_atm_5_analysis(mock_chain, atm_idx, ltp)
    assert result is not None
    assert "immediate_resistance" in result
    assert "immediate_support" in result
    assert "risk_analysis" in result
    assert result["immediate_resistance"]["strike"] == 3920
    assert result["immediate_support"]["strike"] == 3900
    assert result["risk_analysis"]["flag_code"] in ("RISK_UPSIDE_SQUEEZE", "RANGE_LOCK_STABLE", "RISK_DOWNSIDE_FLUSH", "DUAL_UNWIND_VOLATILITY", "NEUTRAL_BALANCED")

def test_compute_atm_5_analysis_cold_start():
    # Market open with zero OI changes
    mock_chain = [
        {"strike": 100, "ce_oi": 1000, "ce_oi_chg": 0, "ce_ltp": 10, "ce_prev_ltp": 10, "pe_oi": 200, "pe_oi_chg": 0, "pe_ltp": 5, "pe_prev_ltp": 5},
        {"strike": 110, "ce_oi": 500, "ce_oi_chg": 0, "ce_ltp": 5, "ce_prev_ltp": 5, "pe_oi": 1000, "pe_oi_chg": 0, "pe_ltp": 10, "pe_prev_ltp": 10},
    ]
    result = compute_atm_5_analysis(mock_chain, 0, 102.0)
    assert result is not None
    assert result["immediate_resistance"]["strike"] == 110
    assert result["immediate_support"]["strike"] == 100
    # Cold start relies on static dominance: 500/(500+1000) = 33%, 200/(200+1000) = 17%
    assert result["immediate_resistance"]["strength_score"] == 33
    assert result["immediate_support"]["strength_score"] == 17

def test_compute_atm_5_air_pocket_risk():
    # Air pocket scenario: Weak PE support at 3900 and almost 0 PE OI for 2-3 strikes below (3880, 3860)
    mock_chain = [
        {"strike": 3840, "ce_oi": 5000, "ce_oi_chg": 100, "ce_ltp": 90, "ce_prev_ltp": 80, "pe_oi": 100, "pe_oi_chg": -50, "pe_ltp": 20, "pe_prev_ltp": 25},
        {"strike": 3850, "ce_oi": 5000, "ce_oi_chg": 100, "ce_ltp": 80, "ce_prev_ltp": 70, "pe_oi": 10, "pe_oi_chg": -10, "pe_ltp": 25, "pe_prev_ltp": 30},
        {"strike": 3860, "ce_oi": 5000, "ce_oi_chg": 100, "ce_ltp": 70, "ce_prev_ltp": 60, "pe_oi": 10, "pe_oi_chg": -10, "pe_ltp": 30, "pe_prev_ltp": 35},
        {"strike": 3880, "ce_oi": 5000, "ce_oi_chg": 100, "ce_ltp": 60, "ce_prev_ltp": 50, "pe_oi": 10, "pe_oi_chg": -10, "pe_ltp": 35, "pe_prev_ltp": 40},
        {"strike": 3900, "ce_oi": 5000, "ce_oi_chg": 1000, "ce_ltp": 50, "ce_prev_ltp": 40, "pe_oi": 500, "pe_oi_chg": -200, "pe_ltp": 40, "pe_prev_ltp": 45}, # Weak support
        {"strike": 3920, "ce_oi": 3000, "ce_oi_chg": 100, "ce_ltp": 40, "ce_prev_ltp": 30, "pe_oi": 100, "pe_oi_chg": 10, "pe_ltp": 50, "pe_prev_ltp": 55},
    ]
    result = compute_atm_5_analysis(mock_chain, 4, 3907.60)
    assert result is not None
    assert result["risk_analysis"]["flag_code"] == "AIR_POCKET_DOWNSIDE"

def test_compute_atm_5_zero_oi_skipping():
    # TVSMOTOR scenario: LTP is 3949.70. Intermediate strikes 3940 and 3920 have 0 PE OI.
    # Nearest PE strike with positive OI is 3900 (PE OI: 307000).
    mock_chain = [
        {"strike": 3800, "ce_oi": 233000, "ce_oi_chg": 0, "ce_ltp": 156.4, "ce_prev_ltp": 156.4, "pe_oi": 320000, "pe_oi_chg": -23450, "pe_ltp": 6.2, "pe_prev_ltp": 6.2},
        {"strike": 3850, "ce_oi": 39000, "ce_oi_chg": 0, "ce_ltp": 120.6, "ce_prev_ltp": 120.6, "pe_oi": 178000, "pe_oi_chg": 12600, "pe_ltp": 12.1, "pe_prev_ltp": 12.1},
        {"strike": 3900, "ce_oi": 234000, "ce_oi_chg": 0, "ce_ltp": 75.8, "ce_prev_ltp": 75.8, "pe_oi": 307000, "pe_oi_chg": 52900, "pe_ltp": 24.3, "pe_prev_ltp": 24.3},
        {"strike": 3920, "ce_oi": 93300, "ce_oi_chg": 0, "ce_ltp": 59.8, "ce_prev_ltp": 59.8, "pe_oi": 0, "pe_oi_chg": 0, "pe_ltp": 0.0, "pe_prev_ltp": 0.0},
        {"strike": 3940, "ce_oi": 162000, "ce_oi_chg": 0, "ce_ltp": 49.7, "ce_prev_ltp": 49.7, "pe_oi": 0, "pe_oi_chg": 0, "pe_ltp": 0.0, "pe_prev_ltp": 0.0},
        {"strike": 3950, "ce_oi": 188000, "ce_oi_chg": 0, "ce_ltp": 44.7, "ce_prev_ltp": 44.7, "pe_oi": 74900, "pe_oi_chg": 17800, "pe_ltp": 45.6, "pe_prev_ltp": 45.6},
        {"strike": 3960, "ce_oi": 123000, "ce_oi_chg": 0, "ce_ltp": 39.7, "ce_prev_ltp": 39.7, "pe_oi": 42900, "pe_oi_chg": 8200, "pe_ltp": 50.1, "pe_prev_ltp": 50.1},
        {"strike": 4000, "ce_oi": 501000, "ce_oi_chg": 0, "ce_ltp": 25.0, "ce_prev_ltp": 25.0, "pe_oi": 52500, "pe_oi_chg": 14300, "pe_ltp": 75.1, "pe_prev_ltp": 75.1},
    ]
    ltp = 3949.70
    atm_idx = 5  # 3950 strike

    result = compute_atm_5_analysis(mock_chain, atm_idx, ltp)
    assert result is not None
    # Immediate support must skip 3940 and 3920 (0 PE OI) and pick 3900!
    assert result["immediate_support"]["strike"] == 3900
    assert result["immediate_support"]["oi"] == 307000


