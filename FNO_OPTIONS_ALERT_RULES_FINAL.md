# FNO Intraday Options Scanner — Final Alert Rules
## Complete Pre-Computation + All Gates + All Fine-Tuning Applied

---

## DESIGN PRINCIPLES

```
1. Volume + Price Movement + Direction + Structure + Market Context = Signal
2. Any one missing = No alert
3. All computation runs on Kite data only — zero external dependencies
4. Gates eliminate noise. Tags add context. Neither substitutes for the other.
5. Every alert must be actionable by a retail options trader within 2–3 minutes
```

---

## DATA DEPENDENCIES
*Everything below runs on these inputs only — no external APIs required.*

```
Kite WebSocket ticks      → live OHLCV per symbol
Kite historical_data()    → 10-day 1-min history (fetched pre-market)
Kite instruments()        → token map, prev day OHLC
BaselineFetcher           → ATR, time-slot median vol, 5-min avg vol,
                            PDH, PDL, PDC, ORB high/low
Options chain feed        → IV Rank (optional — gates degrade gracefully if absent)
```

---

## PRE-COMPUTATION
*Runs once per candle close, per symbol. All downstream gates read from these values only.*

```python
# ═════════════════════════════════════════════════════════════════════
# SECTION A — CANDLE BASICS
# ═════════════════════════════════════════════════════════════════════

candle_range        = candle.high - candle.low
body                = abs(candle.close - candle.open)
body_ratio          = body / candle_range if candle_range > 0 else 0
wick_ratio          = 1.0 - body_ratio
direction           = "BULL" if candle.close > candle.open else "BEAR"


# ═════════════════════════════════════════════════════════════════════
# SECTION B — VOLUME  (weighted recent baseline)
# ═════════════════════════════════════════════════════════════════════

slot                = candle.minute.strftime("%H:%M")

# Weighted baseline — 60% last 3 days + 40% prior 7 days
# Adapts faster to post-event regime and new trends
# without being distorted by a single outlier session
recent_vols         = slot_volumes[symbol][slot][-3:]
older_vols          = slot_volumes[symbol][slot][-10:-3]
recent_median       = statistics.median(recent_vols) if recent_vols else 0
older_median        = statistics.median(older_vols)  if older_vols  else 0
baseline_vol        = (
    (0.60 * recent_median + 0.40 * older_median)
    if older_median > 0 else recent_median
)
vol_ratio           = candle.volume / baseline_vol if baseline_vol > 0 else 0

# 5-min cumulative volume
avg_5min_vol        = baseline[symbol].get("5min_avg", 0)
cum5_vol            = sum(last_5_closed_1min_volumes[symbol])

# Liquidity-tiered cum5 threshold
# Same multiplier for all stocks is too strict for mid-cap FnO
avg_daily_vol       = sum(v for k, v in baseline[symbol].items()
                          if k not in ("5min_avg",))
cum5_threshold      = (
    1.8 if avg_daily_vol > 5_000_000 else   # large-cap liquid
    1.5 if avg_daily_vol > 1_000_000 else   # mid liquidity
    1.3                                      # low liquidity mid-cap FnO
)
cum5_confirmed      = (
    cum5_vol >= cum5_threshold * avg_5min_vol
    if avg_5min_vol > 0 else True
)


# ═════════════════════════════════════════════════════════════════════
# SECTION C — ATR & INSTRUMENT TYPE
# ═════════════════════════════════════════════════════════════════════

atr                 = atr_14[symbol]    # 14-period ATR from BaselineFetcher
is_index            = symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


# ═════════════════════════════════════════════════════════════════════
# SECTION D — PREVIOUS DAY LEVELS  (PDH / PDL / PDC)
# ═════════════════════════════════════════════════════════════════════

# Fetched from Kite instruments() or historical_data() during baseline fetch
pdh                 = prev_day_high[symbol]     # previous day high
pdl                 = prev_day_low[symbol]      # previous day low
pdc                 = prev_day_close[symbol]    # previous day close

# Level break flags — first clean break only
breaks_pdh          = (candle.close > pdh and prev_candle.close <= pdh)
breaks_pdl          = (candle.close < pdl and prev_candle.close >= pdl)
near_pdh            = abs(candle.close - pdh) / atr <= 0.3   # within 0.3 ATR
near_pdl            = abs(candle.close - pdl) / atr <= 0.3


# ═════════════════════════════════════════════════════════════════════
# SECTION E — OPENING RANGE BREAKOUT (ORB)
# ═════════════════════════════════════════════════════════════════════

# Opening range = 09:15–09:30 (first 15 minutes)
# Computed once after 09:30, cached for rest of session
orb_high            = max(c.high for c in today_candles[symbol]
                          if time(9,15) <= c.minute.time() <= time(9,30))
orb_low             = min(c.low  for c in today_candles[symbol]
                          if time(9,15) <= c.minute.time() <= time(9,30))
orb_range           = orb_high - orb_low

# ORB break flags — first clean break above/below only
orb_break_bull      = (candle.close > orb_high and
                       prev_candle.close <= orb_high and
                       t > time(9, 30))         # only valid after ORB period closes
orb_break_bear      = (candle.close < orb_low  and
                       prev_candle.close >= orb_low and
                       t > time(9, 30))

# ORB context tag — valid until 12:00 PM (ORB setups lose relevance after midday)
orb_context_valid   = t <= time(12, 0)


# ═════════════════════════════════════════════════════════════════════
# SECTION F — GAP CONTEXT
# ═════════════════════════════════════════════════════════════════════

# Gap = difference between today's first candle open and previous day close
open_915            = today_candles[symbol][0].open    # 09:15 candle open
gap_pct             = (open_915 - pdc) / pdc * 100

is_gap_up           = gap_pct >  0.5     # gapped up more than 0.5%
is_gap_down         = gap_pct < -0.5     # gapped down more than 0.5%
is_gap_day          = is_gap_up or is_gap_down

# Gap fill — is price moving back toward previous close?
gap_filled          = (
    (is_gap_up   and candle.close <= pdc) or
    (is_gap_down and candle.close >= pdc)
)
gap_filling         = (
    (is_gap_up   and candle.close < open_915 and not gap_filled) or
    (is_gap_down and candle.close > open_915 and not gap_filled)
)
# gap_filling = price moving toward fill but not yet complete
# Used to suppress false breakout alerts during gap fill moves


# ═════════════════════════════════════════════════════════════════════
# SECTION G — ADAPTIVE STRUCTURAL BREAK (replaces fixed 20-candle)
# ═════════════════════════════════════════════════════════════════════

# Expand lookback until window covers at least 1.0×ATR of price range
# Min 10 candles, max 30 candles
# Prevents Gate 0 being trivial in fast sessions or useless in slow ones
def adaptive_lookback(history, atr, min_n=10, max_n=30):
    for n in range(min_n, max_n + 1):
        w = history[-n:]
        if (max(c.high for c in w) - min(c.low for c in w)) >= 1.0 * atr:
            return n
    return max_n

lookback_n          = adaptive_lookback(history, atr)
high_n              = max(c.high for c in history[-lookback_n:])
low_n               = min(c.low  for c in history[-lookback_n:])
broke_high          = candle.close > high_n
broke_low           = candle.close < low_n
break_distance      = (
    abs(candle.close - high_n) if broke_high else
    abs(candle.close - low_n)  if broke_low  else 0
)


# ═════════════════════════════════════════════════════════════════════
# SECTION H — MARKET STRUCTURE: BOS & CHOCH
# ═════════════════════════════════════════════════════════════════════

# Swing point detection — left=3, right=3 (3 candles each side)
def detect_swing_highs(history, left=3, right=3):
    swings = []
    for i in range(left, len(history) - right):
        c = history[i]
        if (all(c.high > history[i-j].high for j in range(1, left+1)) and
                all(c.high > history[i+j].high for j in range(1, right+1))):
            swings.append(c)
    return swings

def detect_swing_lows(history, left=3, right=3):
    swings = []
    for i in range(left, len(history) - right):
        c = history[i]
        if (all(c.low < history[i-j].low for j in range(1, left+1)) and
                all(c.low < history[i+j].low for j in range(1, right+1))):
            swings.append(c)
    return swings

swing_highs         = detect_swing_highs(history)
swing_lows          = detect_swing_lows(history)

last_sh             = swing_highs[-1] if swing_highs         else None
prev_sh             = swing_highs[-2] if len(swing_highs)>=2 else None
last_sl             = swing_lows[-1]  if swing_lows          else None
prev_sl             = swing_lows[-2]  if len(swing_lows) >=2 else None

# Structural trend from swing sequence
if prev_sh and last_sh and prev_sl and last_sl:
    hh              = last_sh.high > prev_sh.high   # higher high
    hl              = last_sl.low  > prev_sl.low    # higher low
    lh              = last_sh.high < prev_sh.high   # lower high
    ll              = last_sl.low  < prev_sl.low    # lower low
    struct_trend    = (
        "UP"   if hh and hl else
        "DOWN" if lh and ll else
        "CHOP"
    )
else:
    struct_trend    = "UNKNOWN"

# BOS — Break of Structure (continuation)
# Price breaks last swing high in uptrend or last swing low in downtrend
bos_bull            = (
    struct_trend == "UP"   and
    last_sh is not None    and
    candle.close > last_sh.high and
    prev_candle.close <= last_sh.high    # first break only
)
bos_bear            = (
    struct_trend == "DOWN" and
    last_sl is not None    and
    candle.close < last_sl.low  and
    prev_candle.close >= last_sl.low
)

# CHOCH — Change of Character (reversal)
# Price breaks AGAINST the structural trend for the first time
choch_bull          = (
    struct_trend == "DOWN" and           # was in downtrend
    last_sh is not None    and
    candle.close > last_sh.high and      # first break above swing high
    prev_candle.close <= last_sh.high
)
choch_bear          = (
    struct_trend == "UP"   and           # was in uptrend
    last_sl is not None    and
    candle.close < last_sl.low  and      # first break below swing low
    prev_candle.close >= last_sl.low
)

bos_triggered       = bos_bull   or bos_bear
choch_triggered     = choch_bull or choch_bear
struct_break        = bos_triggered or choch_triggered


# ═════════════════════════════════════════════════════════════════════
# SECTION I — OPTIONS PREMIUM ESTIMATE  (three-tier delta)
# ═════════════════════════════════════════════════════════════════════

est_premium_atm     = body * 0.50    # ATM  — most common retail buy
est_premium_itm     = body * 0.65    # slightly ITM — after breakout moved
est_premium_otm     = body * 0.30    # slight OTM — popular with retail

# Gate uses ATM as the viability check
premium_min         = (
    1.50 if candle.close <  500  else
    3.00 if candle.close < 2000  else
    5.00
)
if is_expiry_day:
    premium_min     = premium_min * 0.50     # Gamma compensates on expiry


# ═════════════════════════════════════════════════════════════════════
# SECTION J — TREND & MARKET QUALITY
# ═════════════════════════════════════════════════════════════════════

sma5                = mean(last_5_closed_5min_candle_closes[symbol])

# Trend alignment: use structural trend if known, else SMA5
if struct_trend in ("UP", "DOWN"):
    trend_aligned   = (
        (direction == "BULL" and struct_trend == "UP")   or
        (direction == "BEAR" and struct_trend == "DOWN")
    )
else:
    # Fallback to SMA5 when structure not established
    trend_aligned   = (
        (direction == "BULL" and candle.close > sma5) or
        (direction == "BEAR" and candle.close < sma5)
    )

chop_14             = dreiss_choppiness_index(period=14)
linearity           = path_efficiency_score(period=20)


# ═════════════════════════════════════════════════════════════════════
# SECTION K — BOLLINGER BANDS  (minimum history guard)
# ═════════════════════════════════════════════════════════════════════

# Minimum 60 candles (~1 hour) before BB percentile distribution is meaningful
# Prevents false squeeze tags in the first hour of the session
MIN_HISTORY_FOR_SQUEEZE = 60

closes_50           = [c.close for c in history[-50:]]
bb_widths_50        = rolling_bb_widths(closes_50, period=20, std=2.0)
bb_width_now        = bb_widths_50[-1] if bb_widths_50 else 0
bb_width_pct        = percentile_rank(bb_width_now, bb_widths_50)
bb_expanding        = (
    bb_width_now > mean(bb_widths_50[-5:])
    if len(bb_widths_50) >= 5 else False
)
squeeze_eligible    = (
    len(history) >= MIN_HISTORY_FOR_SQUEEZE and
    bb_width_pct < 15 and
    bb_expanding
)


# ═════════════════════════════════════════════════════════════════════
# SECTION L — EXHAUSTION REFERENCE
#             Day high/low for stocks — VWAP deviation for index
# ═════════════════════════════════════════════════════════════════════

day_high            = max(c.high for c in today_candles[symbol])
day_low             = min(c.low  for c in today_candles[symbol])

if is_index:
    vwap            = cumulative_typical_x_vol[symbol] / cumulative_vol[symbol]
    vwap_dev        = abs(candle.close - vwap) / atr if atr > 0 else 0
    exhaustion_ref  = vwap_dev >= 1.5
else:
    vwap            = None
    vwap_dev        = None
    # Stock exhaustion uses day extreme proximity instead of VWAP
    # VWAP accumulates error on trending stocks — day high/low is always visible
    exhaustion_ref  = (
        candle.high >= day_high if direction == "BULL"
        else candle.low <= day_low
    )

bull_exhaustion     = vol_ratio >= 3.5 and wick_ratio >= 0.50 and exhaustion_ref and direction == "BULL"
bear_exhaustion     = vol_ratio >= 3.5 and wick_ratio >= 0.50 and exhaustion_ref and direction == "BEAR"
exhaustion_triggered= bull_exhaustion or bear_exhaustion


# ═════════════════════════════════════════════════════════════════════
# SECTION M — VWAP RECLAIM / REJECTION  (index only)
# ═════════════════════════════════════════════════════════════════════

if is_index:
    vwap_reclaim    = (
        prev_candle.close <  vwap and
        candle.close      >  vwap and
        vol_ratio         >= 2.0
    )
    vwap_rejection  = (
        prev_candle.close >  vwap and
        candle.close      <  vwap and
        vol_ratio         >= 2.0
    )
else:
    vwap_reclaim    = False
    vwap_rejection  = False


# ═════════════════════════════════════════════════════════════════════
# SECTION N — IV CONTEXT  (optional — degrades gracefully if absent)
# ═════════════════════════════════════════════════════════════════════

iv_rank             = (current_iv / iv_52w_high * 100) if iv_52w_high > 0 else None
iv_expanding        = current_iv > prev_candle_iv if iv_rank else None
iv_available        = iv_rank is not None


# ═════════════════════════════════════════════════════════════════════
# SECTION O — EXPIRY CONTEXT
# ═════════════════════════════════════════════════════════════════════

days_to_expiry      = (weekly_expiry_date - today).days
is_expiry_day       = days_to_expiry == 0
gamma_zone          = days_to_expiry <= 1


# ═════════════════════════════════════════════════════════════════════
# SECTION P — TIME FLAGS & TIME-ADJUSTED THRESHOLDS
# ═════════════════════════════════════════════════════════════════════

t                   = candle.minute.time()

is_open_window      = time(9, 20)  <= t <= time(9, 30)
is_close_window     = time(15, 15) <= t <= time(15, 25)
is_event_window     = is_open_window or is_close_window

# Body ratio — relaxed at session open and close where wicks are naturally larger
body_ratio_min      = (
    0.35 if time(9, 20)  <= t < time(10, 0)  else   # opening range
    0.40 if time(10, 0)  <= t < time(14, 0)  else   # mid-session
    0.35 if time(14, 0)  <= t <= time(15, 25) else  # pre-close
    0.40
)

# Range minimum — relaxed on expiry (Gamma amplifies small moves)
range_min           = 0.3 * atr if is_expiry_day else 0.5 * atr
```

---

## STEP 1 — MARKET HOURS GATE

```python
if not (time(9, 20) <= t <= time(15, 25)):
    DISCARD

if is_expiry_day and not (time(10, 0) <= t <= time(14, 30)):
    DISCARD
```

| Boundary | Value | Reason |
|----------|-------|--------|
| Normal start | 09:20 | IV spike from overnight gap inflates premium in first 5 min |
| Normal end | 15:25 | ATM bid-ask spreads widen to 2–5 pts after this |
| Expiry start | 10:00 | Pre-10 on expiry — OTM premiums are binary lottery tickets |
| Expiry end | 14:30 | OTM options approach zero. No breakout justifies entry after this |

---

## STEP 2 — GAP FILTER GATE (New)

```python
# Suppress breakout alerts when a gap fill move is in progress
# A stock filling its gap looks like a breakout but is just price discovery
if gap_filling and not gap_filled:
    DISCARD

# On large gap days, tag all alerts for context even if not suppressed
if is_gap_day:
    alert["gap_context"] = (
        f"Gap {'up' if is_gap_up else 'down'} {abs(gap_pct):.1f}% "
        f"from prev close ₹{pdc}"
    )
```

| Condition | Action | Reason |
|-----------|--------|--------|
| gap_filling = True | DISCARD | Stock moving toward gap fill — not a structural breakout. Retail buying CE on a gap fill gets trapped immediately |
| gap_filled = True | Allow — tag alert | Gap now fully filled. Any subsequent breakout is genuine structural move |
| is_gap_day but not filling | Allow — tag alert | Price has stabilised post-gap. Tag so retail trader is aware of gap context |
| No gap | Normal pipeline | Proceed |

---

## STEP 3 — STRUCTURAL BREAK GATE (Gate 0)

```python
if not (broke_high or broke_low):
    DISCARD
```

Lookback is adaptive (Section G) — expands 10 to 30 candles until window covers at least 1.0×ATR of price range.

```python
# Structural level tags — added to alert payload
# Priority: PDH/PDL > ORB > Adaptive N-candle high/low

if breaks_pdh or breaks_pdl:
    struct_level_broken = "PDH" if breaks_pdh else "PDL"
    struct_level_quality = "HIGH"
elif orb_break_bull or orb_break_bear:
    struct_level_broken = "ORB"
    struct_level_quality = "HIGH" if orb_context_valid else "MEDIUM"
elif bos_triggered:
    struct_level_broken = "BOS"
    struct_level_quality = "HIGH"
elif choch_triggered:
    struct_level_broken = "CHOCH"
    struct_level_quality = "HIGH"
else:
    struct_level_broken = f"{lookback_n}C_HIGH" if broke_high else f"{lookback_n}C_LOW"
    struct_level_quality = "NORMAL"

alert["struct_level"]   = struct_level_broken
alert["struct_quality"] = struct_level_quality
```

| Level Broken | Quality | Options Meaning |
|---|---|---|
| PDH / PDL | HIGH | Most-watched level by all retail traders. Breakout well-known and reliable |
| ORB (before noon) | HIGH | Opening range breakout — high follow-through probability before 12:00 PM |
| ORB (after noon) | MEDIUM | ORB loses relevance after midday |
| BOS | HIGH | Structural trend continuation confirmed |
| CHOCH | HIGH | First sign of trend reversal — early and high value |
| N-candle high/low | NORMAL | Generic recent level — baseline signal |

---

## STEP 4 — VOLUME GATE (Gate 1)

```python
if vol_ratio < 2.0:
    DISCARD
```

Baseline is weighted (Section B) — 60% last 3 days + 40% prior 7 days.

| Why | Options Reason |
|-----|----------------|
| 2.0× minimum | MM Delta hedging confirms premium expansion. Sub-2× = MMs passive = premium muted |
| Weighted baseline | Adapts after events and new trends. Flat median breaks post-results for 3+ days |

---

## STEP 5 — PRICE MOVEMENT GATE (Gate 2)

```python
# Gate 2A — Range confirmation
if candle_range < range_min:     # 0.5×ATR normal / 0.3×ATR expiry
    DISCARD

# Gate 2B — Estimated premium viability (ATM basis)
if est_premium_atm < premium_min:
    DISCARD
```

| Parameter | Normal | Expiry |
|-----------|--------|--------|
| Range minimum | 0.5× ATR | 0.3× ATR |
| Premium min (< ₹500) | ₹1.50 | ₹0.75 |
| Premium min (₹500–₹2000) | ₹3.00 | ₹1.50 |
| Premium min (> ₹2000) | ₹5.00 | ₹2.50 |

---

## STEP 6 — DIRECTION CONVICTION GATE (Gate 3)

```python
if body_ratio < body_ratio_min:    # 0.35 open/close / 0.40 mid-session
    DISCARD
```

| Time | Threshold | Reason |
|------|-----------|--------|
| 09:20–10:00 | 0.35 | Opening range wicks naturally large |
| 10:00–14:00 | 0.40 | Standard mid-session filter |
| 14:00–15:25 | 0.35 | Pre-close activity expands wicks again |

---

## STEP 7 — IV RANK ADVISORY (Gate 1B)
*No hard discard. Changes suggested trade structure only.*

```python
if iv_available:

    if iv_rank > 85:
        option_action   = "Bull Call Spread" if direction == "BULL" else "Bear Put Spread"
        iv_note         = (f"IV Rank {iv_rank:.0f} — naked buy very expensive. "
                           "Spread limits Vega risk.")

    elif iv_rank > 65:
        iv_note         = (f"IV Rank {iv_rank:.0f} — elevated. "
                           "Consider spread if premium feels heavy.")

    elif iv_rank < 20:
        iv_note         = (f"IV Rank {iv_rank:.0f} — low IV. "
                           "Naked buy favourable. Vega working for you.")
    else:
        iv_note         = None
```

---

## STEP 8 — EVENT WINDOW CLASSIFICATION

```python
if is_event_window and vol_ratio >= 3.5:
    category        = "EVENT_SPIKE"
    priority        = "P4"
    option_action   = "OBSERVE ONLY"
    caution         = ("Opening gap IV or closing auction distortion. "
                       "Wait 2–3 candles for IV to settle before considering entry.")
    → DELIVER
    → SKIP Steps 9–11
    → PROCEED to Step 12 (cooldown) → Step 13 (delivery)
```

---

## STEP 9 — EXPIRY DAY OVERRIDE

```python
if is_expiry_day:
    # Gate 2 thresholds already relaxed in pre-computation (Section I & P)

    if category == "SQUEEZE_BREAKOUT":
        priority        = "P0"
        gamma_note      = ("Expiry day squeeze — ATM option can 3–5× on sustained move. "
                           "ATM strike only. Exit within 2–3 candles.")
```

---

## STEP 10 — CATEGORY CLASSIFICATION
*Evaluated in strict priority order. First match wins.*

---

### 10A — SQUEEZE_BREAKOUT + BOS  (Highest Quality — P0/P1)

```python
if squeeze_eligible and (broke_high or broke_low) and cum5_confirmed and bos_triggered:

    category        = "SQUEEZE_BREAKOUT"
    struct_tag      = "SQUEEZE + BOS"    # highest conviction combination
    priority        = "P0" if is_expiry_day else "P1"

    option_action   = "ATM CE" if direction == "BULL" else "ATM PE"
    entry_note      = ("Squeeze releasing into BOS — strongest confirmation. "
                       "Enter at open of next candle.")
    sl_note         = "SL: close back inside breakout level"
    exit_note       = "Target: 1.5–2× premium. Do not hold through IV crush."
```

---

### 10B — SQUEEZE_BREAKOUT  (P1)

```python
elif squeeze_eligible and (broke_high or broke_low) and cum5_confirmed:

    category        = "SQUEEZE_BREAKOUT"
    struct_tag      = "SQUEEZE"
    priority        = "P0" if is_expiry_day else "P1"

    option_action   = "ATM CE" if direction == "BULL" else "ATM PE"
    entry_note      = "Enter at open of next candle — premium actively expanding"
    sl_note         = "SL: close back inside breakout level"
    exit_note       = "Target: 1.5–2× premium. Exit before IV crush on reversal."

# Squeeze present but 5-min vol not sustained → downgrade
elif squeeze_eligible and (broke_high or broke_low) and not cum5_confirmed:
    category        = "NORMAL_BREAKOUT"
    downgrade_reason= ("Squeeze pattern present but 5-min volume not sustained — "
                       "single 1-min burst. Downgraded to NORMAL_BREAKOUT.")
```

---

### 10C — CHOCH_REVERSAL  (P1)

```python
elif choch_triggered and vol_ratio >= 2.0:

    category        = "CHOCH_REVERSAL"
    priority        = "P1"

    # CHOCH direction is opposite to prior trend
    reversal_dir    = "BULL" if choch_bull else "BEAR"
    option_action   = "ATM CE" if reversal_dir == "BULL" else "ATM PE"
    entry_note      = ("First structural break against prior trend — earliest reversal signal. "
                       "Wait 1 confirmation candle then enter.")
    sl_note         = "SL: close back below/above the CHOCH level"
    caution         = ("Reversal trade — prior trend must be clearly established. "
                       "Check struct_trend = DOWN for CHOCH_BULL or UP for CHOCH_BEAR.")
```

---

### 10D — EXHAUSTION_SPIKE + CHOCH  (Highest Conviction Reversal — P1)

```python
elif exhaustion_triggered and choch_triggered:

    category        = "EXHAUSTION_SPIKE"
    struct_tag      = "EXHAUSTION + CHOCH"   # strongest reversal combination
    priority        = "P1"

    reversal_dir    = "BULL" if bear_exhaustion else "BEAR"
    option_action   = "ATM CE" if reversal_dir == "BULL" else "ATM PE"
    entry_note      = ("Exhaustion at extreme + structural CHOCH confirmation. "
                       "Strongest reversal signal. "
                       "Enter after 1 confirmation candle closes.")
    caution         = ("Trade OPPOSITE to spike candle direction. "
                       "If IV Rank > 60 consider spread over naked buy.")
```

---

### 10E — EXHAUSTION_SPIKE  (P2)

```python
elif exhaustion_triggered:

    category        = "EXHAUSTION_SPIKE"
    priority        = "P2"

    # Trade direction is OPPOSITE to spike candle
    option_action   = "ATM PE" if direction == "BULL" else "ATM CE"
    entry_note      = "Wait 1–2 candles for reversal confirmation before entering"
    caution         = ("DO NOT enter in direction of spike candle. "
                       "Spike direction is the trap. Trade OPPOSITE side.")
    iv_note         = (("If IV Rank > 60, consider spread — "
                        "IV may crush on reversal.") if iv_available and iv_rank > 60
                       else None)
```

---

### 10F — BOS_BREAKOUT  (P1)

```python
elif bos_triggered and vol_ratio >= 2.0:

    category        = "BOS_BREAKOUT"
    priority        = "P1"

    option_action   = "ATM CE" if direction == "BULL" else "ATM PE"
    entry_note      = ("Structural BOS in trend direction — "
                       "trend continuation confirmed. Enter at open of next candle.")
    sl_note         = "SL: close back below BOS level (last swing high/low)"
```

---

### 10G — VWAP_SIGNAL  (P2 — Index Only)

```python
elif is_index and (vwap_reclaim or vwap_rejection):

    category        = "VWAP_SIGNAL"
    priority        = "P2"
    signal_type     = "RECLAIM" if vwap_reclaim else "REJECTION"

    option_action   = "ATM CE" if vwap_reclaim else "ATM PE"
    entry_note      = (
        f"VWAP {signal_type} with volume — "
        f"{'bullish reclaim' if vwap_reclaim else 'bearish rejection'}. "
        "Enter at open of next candle."
    )
    sl_note         = f"SL: close back {'below' if vwap_reclaim else 'above'} VWAP"
```

---

### 10H — ORB_BREAKOUT  (P1 before noon / P2 after noon)

```python
elif (orb_break_bull or orb_break_bear) and vol_ratio >= 2.0:

    category        = "ORB_BREAKOUT"
    priority        = "P1" if orb_context_valid else "P2"

    option_action   = "ATM CE" if orb_break_bull else "ATM PE"
    entry_note      = (
        "Opening Range Breakout — high follow-through probability before noon. "
        if orb_context_valid else
        "ORB break after noon — lower reliability."
    )
    sl_note         = f"SL: close back {'below ORB high' if orb_break_bull else 'above ORB low'} ₹{orb_high if orb_break_bull else orb_low:.2f}"
```

---

### 10I — NORMAL_BREAKOUT  (P2 / P1 if PDH/PDL)

```python
else:
    # Default — reached when no specific pattern matched
    # All conditions already confirmed by earlier gates

    category        = "NORMAL_BREAKOUT"

    # Upgrade to P1 if breaking a major level
    if breaks_pdh or breaks_pdl:
        priority    = "P1"
        entry_note  = ("Breaking Previous Day High/Low — "
                       "major level watched by all market participants.")
    else:
        priority    = "P2"
        entry_note  = ("Enter early next candle. "
                       "Skip if premium already up >20% from pre-candle level.")

    option_action   = (
        "ATM CE or slight OTM CE" if direction == "BULL"
        else "ATM PE or slight OTM PE"
    )
    sl_note         = "SL: close back below/above breakout level"
```

---

## STEP 11 — CONVICTION GATE (Gate 5)

**Applied to: SQUEEZE_BREAKOUT, BOS_BREAKOUT, NORMAL_BREAKOUT, ORB_BREAKOUT.**
**Exempt: EXHAUSTION_SPIKE, CHOCH_REVERSAL, VWAP_SIGNAL, EVENT_SPIKE.**

```python
# Check 1 — Trend Alignment
if not trend_aligned:
    DISCARD

# Check 2 — Choppiness Index  (soft zone — not binary hard cut)
if chop_14 > 61.8:
    DISCARD                              # pure chop — hard discard at Fibonacci level

elif chop_14 > 55.0:
    priority        = downgrade_priority(priority)   # P1→P2, P2 stays P2
    chop_warning    = f"Mild chop ({chop_14:.1f}) — lower conviction breakout"
    # Alert still fires at reduced priority

# Check 3 — Linearity  (volume-compensated)
linearity_base      = 0.30 if category == "SQUEEZE_BREAKOUT" else 0.40

# High volume conviction partially offsets linearity requirement
# 6× volume squeeze with 32% linearity > 2.1× volume squeeze with 42% linearity
if vol_ratio >= 4.0:
    linearity_base  = max(0.25, linearity_base - 0.10)

if linearity < linearity_base:
    DISCARD

# Check 4 — Expiry time cutoff
if is_expiry_day and t > time(14, 30):
    DISCARD
```

| Check | Threshold | Notes |
|-------|-----------|-------|
| Trend alignment | struct_trend or SMA5 | Structural trend used when available. SMA5 as fallback |
| CHOP hard discard | > 61.8 | Fibonacci boundary — mathematically meaningful |
| CHOP soft warning | 55–61.8 | Priority downgrade + warning. Binary cut at 55 causes edge cases |
| Linearity — breakout | ≥ 40% | Whipsaw preceding move = premium collapses within 2–3 candles |
| Linearity — squeeze | ≥ 30% | Compression creates low linearity in lookback — penalising twice is wrong |
| Vol ≥ 4× compensation | −0.10, floor 0.25 | Very high vol is itself evidence of conviction |
| Expiry cutoff | 14:30 | OTM options worthless after this on expiry |

**Why CHOCH_REVERSAL is exempt:**
Trend alignment check is incompatible with reversal signals by definition. CHOCH fires precisely when trend is reversing — requiring trend alignment would block 100% of valid CHOCH alerts.

**Why EXHAUSTION_SPIKE is exempt:**
Reversal mean-reversion trade — CHOP and linearity of prior trend are irrelevant. Exhaustion at day extreme with wick rejection is the signal regardless of market choppiness.

**Why VWAP_SIGNAL is exempt:**
VWAP reclaim/rejection is itself a mean-reversion setup for indices. Requiring trend alignment would block the most common VWAP trade setup.

---

## STEP 12 — SYMBOL COOLDOWN GATE (Gate 6)

```python
# Category-aware cooldown — not flat 5 min for all
COOLDOWN = {
    "SQUEEZE_BREAKOUT": 300,     # 5 min
    "BOS_BREAKOUT":     300,     # 5 min
    "ORB_BREAKOUT":     300,     # 5 min — ORB fires once and stays valid
    "NORMAL_BREAKOUT":  300,     # 5 min
    "CHOCH_REVERSAL":   180,     # 3 min — confirmation candle timing
    "EXHAUSTION_SPIKE": 120,     # 2 min — second confirmation candle valuable
    "VWAP_SIGNAL":      180,     # 3 min
    "EVENT_SPIKE":      180,     # 3 min
}

if symbol in last_alert_time:
    elapsed = (now - last_alert_time[symbol]).total_seconds()
    if elapsed < COOLDOWN.get(category, 300):
        DISCARD

last_alert_time[symbol] = now
```

**Per-symbol cooldown not clock-based (minute % 5 == 0):**
Clock gate silently discards valid breakouts at :31–:34 and cannot re-evaluate at :35 because candle conditions have already changed.

---

## STEP 13 — ALERT DELIVERY

```python
alert = {
    # ── Identity
    "symbol":               symbol,
    "time":                 candle.minute.strftime("%H:%M"),
    "alerted_at":           now.strftime("%H:%M:%S"),
    "category":             category,
    "struct_tag":           struct_tag,          # e.g. "SQUEEZE + BOS"
    "priority":             priority,            # P0 / P1 / P2 / P4
    "direction":            direction,           # BULL / BEAR

    # ── Structure context
    "struct_level":         struct_level_broken, # PDH / PDL / ORB / BOS / CHOCH / NC_HIGH
    "struct_quality":       struct_level_quality,# HIGH / MEDIUM / NORMAL
    "struct_trend":         struct_trend,        # UP / DOWN / CHOP / UNKNOWN
    "bos_triggered":        bos_triggered,
    "choch_triggered":      choch_triggered,
    "lookback_used":        lookback_n,

    # ── Previous day levels
    "pdh":                  pdh,
    "pdl":                  pdl,
    "pdc":                  pdc,
    "breaks_pdh":           breaks_pdh,
    "breaks_pdl":           breaks_pdl,

    # ── ORB
    "orb_high":             orb_high,
    "orb_low":              orb_low,
    "orb_break":            orb_break_bull or orb_break_bear,

    # ── Gap context
    "is_gap_day":           is_gap_day,
    "gap_pct":              round(gap_pct, 2),
    "gap_filled":           gap_filled,

    # ── Volume
    "vol_ratio":            round(vol_ratio, 2),
    "cum5_vol":             cum5_vol,
    "cum5_confirmed":       cum5_confirmed,
    "cum5_threshold":       round(cum5_threshold, 1),

    # ── Price
    "open":                 candle.open,
    "high":                 candle.high,
    "low":                  candle.low,
    "close":                candle.close,
    "volume":               candle.volume,
    "candle_range":         round(candle_range, 2),
    "body":                 round(body, 2),
    "body_ratio":           round(body_ratio, 2),
    "break_distance":       round(break_distance, 2),
    "break_distance_pct":   round(break_distance / candle.close * 100, 3),

    # ── Day range
    "day_high":             day_high,
    "day_low":              day_low,

    # ── Market quality
    "atr":                  round(atr, 2),
    "chop":                 round(chop_14, 1),
    "linearity":            round(linearity, 2),
    "trend_aligned":        trend_aligned,
    "sma5":                 round(sma5, 2),
    "chop_warning":         chop_warning if chop_14 > 55 else None,

    # ── BB
    "bb_width_pct":         round(bb_width_pct, 1),
    "bb_expanding":         bb_expanding,
    "squeeze_eligible":     squeeze_eligible,

    # ── VWAP (index only)
    "vwap":                 round(vwap, 2) if is_index else None,
    "vwap_dev":             round(vwap_dev, 2) if is_index else None,
    "vwap_reclaim":         vwap_reclaim if is_index else None,
    "vwap_rejection":       vwap_rejection if is_index else None,

    # ── Options premium
    "est_premium_atm":      round(est_premium_atm, 2),
    "est_premium_itm":      round(est_premium_itm, 2),
    "est_premium_otm":      round(est_premium_otm, 2),

    # ── IV
    "iv_rank":              round(iv_rank, 1) if iv_available else None,
    "iv_expanding":         iv_expanding,

    # ── Expiry
    "days_to_expiry":       days_to_expiry,
    "is_expiry_day":        is_expiry_day,
    "gamma_zone":           gamma_zone,

    # ── Retail guidance
    "option_action":        option_action,
    "entry_note":           entry_note,
    "sl_note":              sl_note,
    "exit_note":            exit_note,
    "caution":              caution,
    "iv_note":              iv_note,
    "gamma_note":           gamma_note if is_expiry_day else None,
    "downgrade_reason":     downgrade_reason if downgrade_reason else None,
    "gap_context":          gap_context if is_gap_day else None,
}
```

---

## COMPLETE PIPELINE FLOW

```
Candle closes
    │
    ├─ STEP 1:  Market hours?
    │           Normal  09:20–15:25              NO → DISCARD
    │           Expiry  10:00–14:30              NO → DISCARD
    │
    ├─ STEP 2:  Gap fill in progress?            YES → DISCARD
    │           (tag gap context if gap day)
    │
    ├─ STEP 3:  Broke adaptive structural level?
    │           (PDH/PDL/ORB/BOS/CHOCH/N-candle)
    │           Tag level quality: HIGH/MEDIUM/NORMAL
    │                                            NO → DISCARD
    │
    ├─ STEP 4:  vol_ratio ≥ 2.0×?
    │           (weighted 60/40 recent baseline)  NO → DISCARD
    │
    ├─ STEP 5:  range ≥ range_min?
    │           est_premium_atm ≥ premium_min?   NO → DISCARD
    │
    ├─ STEP 6:  body_ratio ≥ body_ratio_min?
    │           (0.35 open/close / 0.40 mid)     NO → DISCARD
    │
    ├─ STEP 7:  IV Rank advisory (if available)
    │           > 85 → change to spread, no discard
    │           > 65 → add warning, no discard
    │           < 20 → favourable note
    │
    ├─ STEP 8:  Event window + vol ≥ 3.5×?
    │           YES → EVENT_SPIKE (P4) → skip to Step 12
    │
    ├─ STEP 9:  Expiry override?
    │           YES → thresholds already relaxed in pre-compute
    │                 SQUEEZE → P0
    │
    ├─ STEP 10: Classify (first match wins):
    │
    │           squeeze_eligible + broke + cum5 + BOS  → SQUEEZE+BOS   (P0/P1)
    │           squeeze_eligible + broke + cum5         → SQUEEZE       (P0/P1)
    │           squeeze_eligible + broke, cum5 fail     → NORMAL        (downgrade)
    │           choch + vol ≥ 2×                        → CHOCH_REVERSAL(P1)
    │           exhaustion + choch                      → EXHAUST+CHOCH (P1)
    │           exhaustion                              → EXHAUSTION    (P2)
    │           bos + vol ≥ 2×                          → BOS_BREAKOUT  (P1)
    │           index + vwap reclaim/rejection          → VWAP_SIGNAL   (P2)
    │           orb break + vol ≥ 2×                    → ORB_BREAKOUT  (P1/P2)
    │           breaks PDH/PDL                          → NORMAL (P1)
    │           default                                 → NORMAL (P2)
    │
    ├─ STEP 11: Conviction Gate
    │           (SQUEEZE / BOS / ORB / NORMAL only — exempt: EXHAUST/CHOCH/VWAP/EVENT)
    │
    │           trend_aligned?                    NO        → DISCARD
    │           chop > 61.8?                      YES       → DISCARD
    │           chop 55–61.8?                     YES       → lower priority + warning
    │           linearity ≥ threshold?
    │               vol≥4× relaxes by 0.10, floor 0.25      NO → DISCARD
    │           expiry + time > 14:30?            YES       → DISCARD
    │
    ├─ STEP 12: Category-aware cooldown:
    │           SQUEEZE/BOS/ORB/NORMAL < 300s     YES → DISCARD
    │           CHOCH/VWAP/EVENT       < 180s     YES → DISCARD
    │           EXHAUSTION             < 120s     YES → DISCARD
    │
    └─ STEP 13: Deliver alert → SocketIO → Dashboard → SQLite
```

---

## CATEGORY SUMMARY

| Category | Priority | Color | Options Trade | Gate 5 | Cooldown |
|----------|----------|-------|---------------|--------|----------|
| SQUEEZE + BOS | P0 (P0 expiry) | 🟢 Bright Green | ATM CE/PE | ✅ | 5 min |
| SQUEEZE_BREAKOUT | P1 (P0 expiry) | 🟢 Green | ATM CE/PE | ✅ | 5 min |
| CHOCH_REVERSAL | P1 | 🟣 Purple | ATM CE/PE opposite | ❌ Exempt | 3 min |
| EXHAUST + CHOCH | P1 | 🔴 Bright Red | ATM CE/PE opposite | ❌ Exempt | 2 min |
| BOS_BREAKOUT | P1 | 🔵 Blue | ATM CE/PE | ✅ | 5 min |
| ORB_BREAKOUT (before noon) | P1 | 🔵 Blue | ATM CE/PE | ✅ | 5 min |
| EXHAUSTION_SPIKE | P2 | 🔴 Red | Opposite side | ❌ Exempt | 2 min |
| VWAP_SIGNAL (index) | P2 | 🟡 Amber | ATM CE/PE | ❌ Exempt | 3 min |
| NORMAL_BREAKOUT (PDH/PDL) | P1 | 🔵 Blue | ATM/slight OTM | ✅ | 5 min |
| NORMAL_BREAKOUT | P2 | 🔵 Light Blue | ATM/slight OTM | ✅ | 5 min |
| ORB_BREAKOUT (after noon) | P2 | ⚪ Grey-Blue | ATM CE/PE | ✅ | 5 min |
| EVENT_SPIKE | P4 | ⚪ Grey | Observe only | ❌ Exempt | 3 min |

---

## COMPLETE THRESHOLD REFERENCE

| Parameter | Value | Notes |
|-----------|-------|-------|
| Scan start (normal) | 09:20 | IV normalises after opening spike |
| Scan end (normal) | 15:25 | Liquidity collapses after this |
| Scan start (expiry) | 10:00 | Pre-10 on expiry is chaotic |
| Scan end (expiry) | 14:30 | OTM options worthless after this |
| Gap suppress | gap_filling = True | Suppress during gap fill, allow after |
| Vol ratio minimum | 2.0× | MM Delta hedging threshold |
| Vol baseline | 60% last 3d + 40% prior 7d | Weighted — adapts to regime changes |
| Range minimum (normal) | 0.5× ATR | Movement confirmation |
| Range minimum (expiry) | 0.3× ATR | Gamma compensates |
| Premium min (< ₹500) | ₹1.50 / ₹0.75 expiry | Economic viability |
| Premium min (₹500–₹2000) | ₹3.00 / ₹1.50 expiry | Economic viability |
| Premium min (> ₹2000) | ₹5.00 / ₹2.50 expiry | Economic viability |
| Body ratio (open/close) | ≥ 0.35 | Opening/closing wicks larger |
| Body ratio (mid-session) | ≥ 0.40 | Standard conviction filter |
| Structural lookback | Adaptive 10–30 candles | Min 1.0×ATR range covered |
| Swing detection | left=3, right=3 | 3 candles each side for swing point |
| ORB period | 09:15–09:30 | First 15 minutes |
| ORB context valid | Before 12:00 | ORB loses relevance after midday |
| PDH/PDL break | First clean break only | prev_candle below, current above |
| Squeeze BB percentile | < 15th | Tight compression required |
| Squeeze min history | 60 candles | Percentile unreliable before this |
| Cum5 threshold (large-cap) | 1.8× avg | High liquidity |
| Cum5 threshold (mid-cap) | 1.5× avg | Medium liquidity |
| Cum5 threshold (small FnO) | 1.3× avg | Low base volume |
| Exhaustion vol | ≥ 3.5× | Extreme panic only |
| Exhaustion wick | ≥ 50% of range | Clear rejection required |
| Exhaustion ref (stock) | At day high or low | VWAP irrelevant for trending stocks |
| Exhaustion ref (index) | VWAP dev ≥ 1.5× ATR | Indices are mean-reverting |
| VWAP signal | Reclaim or rejection | Index only, vol ≥ 2.0× |
| Event open window | 09:20–09:30 | IV inflated |
| Event close window | 15:15–15:25 | Auction distortion |
| Event vol threshold | ≥ 3.5× | Below this = full pipeline |
| IV Rank — spread | > 85 | Change action, no discard |
| IV Rank — warning | > 65 | Add note, no discard |
| IV Rank — favourable | < 20 | Cheap Vega note |
| Trend alignment | struct_trend → SMA5 fallback | Structural trend preferred |
| CHOP hard discard | > 61.8 | Fibonacci boundary |
| CHOP soft warning | 55–61.8 | Priority downgrade |
| Linearity (breakout) | ≥ 40% | Efficient trend |
| Linearity (squeeze) | ≥ 30% | Compression precedes it |
| Vol ≥ 4× compensation | −0.10, floor 0.25 | High vol offsets linearity |
| Expiry conviction cutoff | 14:30 | Hard discard after this |
| Cooldown SQUEEZE/BOS/ORB/NORMAL | 300s | Evaluation + execution time |
| Cooldown CHOCH/VWAP/EVENT | 180s | Moderate suppression |
| Cooldown EXHAUSTION | 120s | Confirmation candle valuable |

---

## WHAT IS DELIBERATELY EXCLUDED

| Feature | Reason |
|---------|--------|
| News / Catalyst awareness | Requires external APIs. NSE scraping unreliable. IV Rank already proxies catalyst events |
| Options chain OI/PCR | Separate feed required. Out of scope for Kite-only pipeline |
| Multi-timeframe confirmation | Adds latency. Structural trend (BOS/CHOCH) already captures MTF context |
| Sector relative volume | Adds complexity without proportional benefit. CHOP gate handles sector chop indirectly |
| Volume Profile / POC | High computation cost for 200 stocks real-time. Out of scope v1 |
| ADX trend strength | CHOP + Linearity + structural trend cover the same ground with less overhead |
