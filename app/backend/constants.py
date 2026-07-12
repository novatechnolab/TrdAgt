"""
constants.py — Named constants for all TradeSignal business logic.

Single place to tune thresholds. All 4 business logic files import from here.
"""

# ── ATR Risk Multipliers ─────────────────────────────────────────────────────
SL_ATR_MULT  = 1.5   # Stop-loss distance from entry
T1_ATR_MULT  = 2.0   # Target 1
T2_ATR_MULT  = 3.0   # Target 2

# ── ADX (Trend Strength) ─────────────────────────────────────────────────────
ADX_STRONG   = 25    # Strong trending market
ADX_MODERATE = 20    # Moderate trend (minimum for options)

# ── RSI Thresholds ───────────────────────────────────────────────────────────
RSI_CALL_MIN = 45    # RSI sweet spot for CALL entries
RSI_CALL_MAX = 65
RSI_PUT_MIN  = 35    # RSI sweet spot for PUT entries
RSI_PUT_MAX  = 55
RSI_EXTREME_LOW  = 30
RSI_EXTREME_HIGH = 70

# ── Volume ───────────────────────────────────────────────────────────────────
VOL_STRONG   = 2.0   # Volume ratio for strong confirmation
VOL_MODERATE = 1.3   # Volume ratio for moderate confirmation
VOL_WEAK     = 1.0   # Minimum acceptable volume ratio

# ── VWAP Deviation ───────────────────────────────────────────────────────────
VWAP_SWEET_ZONE_PCT = 1.5   # % from VWAP for optimal entry
VWAP_MAX_DEVIATION  = 3.0   # % beyond which NO TRADE is recommended

# ── IV (Implied Volatility) ──────────────────────────────────────────────────
IV_MAX_FOR_BUY   = 70    # IV% above which option buying is risky
IV_CHEAP_MAX     = 40    # IV% below which buying is cheap
IV_MODERATE_MAX  = 50

# ── Gap Tiers ────────────────────────────────────────────────────────────────
GAP_MIN_PCT      = 0.25   # Minimum gap to consider
GAP_TIER2_PCT    = 1.0    # Gap ≥ this → Tier 2
GAP_TIER3_PCT    = 3.0    # Gap ≥ this → Tier 3
GAP_TIER4_PCT    = 6.0    # Gap ≥ this → Tier 4
GAP_W_NORMAL     = 0.4    # Default gap weight in scoring blend
GAP_W_STRONG     = 0.5    # Weight for Tier 3+ with strong confirmation
GAP_W_FADE       = 0.3    # Weight when fade scenario detected

# ── Equity Scoring Signal Thresholds ─────────────────────────────────────────
EQUITY_SIGNAL_MIN   = 55   # Minimum score for BULLISH/BEARISH signal
EQUITY_NEUTRAL_MIN  = 45

# ── Options Scoring Signal Thresholds ────────────────────────────────────────
OPTIONS_SIGNAL_STRONG = 75  # ≥ this → STRONG signal
OPTIONS_SIGNAL_NORMAL = 60  # ≥ this → NORMAL signal
OPTIONS_NO_TRADE_MAX  = 40  # ≤ this → NO TRADE

# ── Entry Validator ──────────────────────────────────────────────────────────
VALIDATOR_MIN_CANDLES     = 30   # Minimum candles needed
VALIDATOR_MIN_SCORE       = 55   # Minimum confidence for isValid=True
VALIDATOR_MIN_CONFIRMATIONS = 1
CONSOL_RANGE_SHRINK       = 0.7  # Consolidation body compression ratio
CONSOL_TREND_LOOKBACK     = 5    # Bars to look back for consolidation

# ── Market Session ───────────────────────────────────────────────────────────
SESSION_CLOSE_HOUR   = 14   # After 2:45 PM = late session
SESSION_CLOSE_MINUTE = 45
CIRCUIT_PROXIMITY_PCT = 2.0  # % from circuit limit → NO TRADE
BID_ASK_MAX_SPREAD_PCT = 1.0  # Illiquidity threshold
