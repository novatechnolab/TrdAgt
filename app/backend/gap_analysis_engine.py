"""
gap_analysis_engine.py — Python port of GapAnalysisEngine (gap-analysis-engine.js)
Faithful 1-to-1 port: same scoring rules, same thresholds, same output schema.
"""
from indicators import compute_adx, compute_rsi, compute_volume_ratio
from constants import (GAP_MIN_PCT, GAP_TIER2_PCT, GAP_TIER3_PCT, GAP_TIER4_PCT,
                       GAP_W_NORMAL, GAP_W_STRONG, GAP_W_FADE)


class GapAnalysisEngine:

    def __init__(self):
        # { symbol: { fillRate, totalGaps, filledGaps } }
        self.gap_fill_database = {}
        self.reversal_detector  = {}
        self.initialization_flag = False

    # ── Gap Fill History ──────────────────────────────────────────────────────

    def initialize_gap_fill_history(self, symbol, ohlcv):
        if not symbol or symbol in self.gap_fill_database:
            return
        self.build_gap_fill_history(symbol, ohlcv)
        self.initialization_flag = True

    def build_gap_fill_history(self, symbol, ohlcv):
        if not ohlcv or len(ohlcv) < 10:
            return
        total_gaps = filled_gaps = 0
        for i in range(1, len(ohlcv)):
            prev_close = ohlcv[i-1]['close']
            open_price = ohlcv[i]['open']
            gap_pct = abs((open_price - prev_close) / prev_close * 100)
            if gap_pct < 0.5:
                continue
            total_gaps += 1
            is_gap_up = open_price > prev_close
            if is_gap_up and ohlcv[i]['low'] <= prev_close:
                filled_gaps += 1
            elif not is_gap_up and ohlcv[i]['high'] >= prev_close:
                filled_gaps += 1
        if total_gaps > 0:
            self.gap_fill_database[symbol] = {
                'fillRate':   filled_gaps / total_gaps,
                'totalGaps':  total_gaps,
                'filledGaps': filled_gaps,
            }

    # ── Main Scoring Entry Point ──────────────────────────────────────────────

    def compute_gap_score(self, data):
        closes      = data.get('closes', [])
        snapshot    = data.get('snapshot', {})
        highs       = data.get('highs', [])
        lows        = data.get('lows', [])
        volumes     = data.get('volumes', [])
        fundamentals = data.get('fundamentals', {})
        options_data = data.get('optionsData', {})

        _empty = {'gapTier': 0, 'score': 0, 'override': None,
                  'log': [], 'confirmationStrong': False, 'isFadeScenario': False}

        if not closes or len(closes) < 2:
            return _empty

        last_close = closes[-1]
        prev_close = closes[-2]
        today_open = snapshot.get('open') or last_close

        if not today_open or not prev_close:
            return _empty

        gap_percent = (today_open - prev_close) / prev_close * 100
        abs_gap     = abs(gap_percent)
        is_gap_up   = gap_percent > 0

        confirmation_strong = False
        is_fade_scenario    = False

        # ── LAYER 1 R-01: Gap Tier + Multiplier ──────────────────────────────
        if abs_gap < 0.25:
            return {**_empty, 'log': ['Tier 0: No actionable gap']}
        elif abs_gap <= 1:
            gap_tier, multiplier = 1, 0.6
        elif abs_gap <= 3:
            gap_tier, multiplier = 2, 1.0
        elif abs_gap <= 6:
            gap_tier, multiplier = 3, 0.85
        else:
            gap_tier, multiplier = 4, 0.6

        score         = 0
        log           = [f'Gap Tier: {gap_tier} ({gap_percent:.2f}%)']
        override_trigger = None

        adx            = compute_adx(highs, lows, closes) if highs and lows else 20
        rsi            = compute_rsi(closes)
        catalyst_score = snapshot.get('catalyst_score', 0)
        vix            = snapshot.get('india_vix', 15)

        # ── LAYER 6 Early: Hard Overrides ────────────────────────────────────
        if vix > 30:
            return {**_empty, 'gapTier': gap_tier,
                    'override': 'WATCH',
                    'log': log + ['OV-01: India VIX > 30 (Extreme Risk)'],
                    'confirmationStrong': False, 'isFadeScenario': False}

        if snapshot.get('surveillance_tags'):
            return {**_empty, 'gapTier': gap_tier,
                    'override': 'AVOID',
                    'log': log + ['OV-03: Stock under ASM/ESM'],
                    'confirmationStrong': False, 'isFadeScenario': False}

        # ── Gap Type Classification ───────────────────────────────────────────
        has_long_wick = False
        if highs and lows:
            recent_wicks = [
                max(abs(highs[-(5-i)] - closes[-(5-i)]),
                    abs(closes[-(5-i)] - lows[-(5-i)])) / closes[-(5-i)]
                for i in range(min(5, len(highs)))
            ]
            has_long_wick = any(w > 0.03 for w in recent_wicks)

        vwap_val = snapshot.get('vwap') or snapshot.get('avg_price') or last_close
        gap_type = 'Common'
        if catalyst_score >= 6 and adx > 25 and last_close == vwap_val:
            gap_type = 'Breakaway'
            confirmation_strong = True
        elif adx > 30 and last_close != vwap_val:
            gap_type = 'Runaway'
        elif (is_gap_up and rsi > 78) or (not is_gap_up and rsi < 22):
            if has_long_wick:
                gap_type = 'Exhaustion'
                is_fade_scenario = True
        elif adx < 20:
            gap_type = 'Common'
            is_fade_scenario = True
        log.append(f'Gap Type: {gap_type}')

        # ── R-02: Gap Fill Rate ───────────────────────────────────────────────
        symbol      = data.get('symbol', '')
        gap_history = self.gap_fill_database.get(symbol)
        if gap_history and gap_history['totalGaps'] >= 5:
            fr = gap_history['fillRate']
            if gap_type == 'Common' and fr > 0.7:
                score += -8 if is_gap_up else 8
                is_fade_scenario = True
                log.append(f'R-02: High gap fill rate ({fr*100:.0f}%) — fade bias')
            elif gap_type == 'Breakaway' and fr < 0.3:
                score += 10 if is_gap_up else -10
                confirmation_strong = True
                log.append(f'R-02: Low fill rate ({fr*100:.0f}%) — continuation bias')

        # ── R-04: Catalyst Persistence ───────────────────────────────────────
        if catalyst_score >= 6:
            score += 10 if is_gap_up else -10
            confirmation_strong = True
            log.append(f'R-04: Strong Catalyst ({catalyst_score})')

        # ── LAYER 2 R-05: Pre-Open Order Imbalance ───────────────────────────
        pre_buy  = snapshot.get('pre_open_buy_qty')  or snapshot.get('buy_qty')
        pre_sell = snapshot.get('pre_open_sell_qty') or snapshot.get('sell_qty')
        if pre_buy is not None and pre_sell is not None and (pre_buy + pre_sell) > 0:
            imbalance = (pre_buy - pre_sell) / (pre_buy + pre_sell)
            if is_gap_up:
                if imbalance > 0.5:  score += 10
                elif imbalance < -0.3: score -= 10
            else:
                if imbalance < -0.5: score -= 10
                elif imbalance > 0.3: score += 10
            log.append(f'R-05: Pre-Open Imbalance ({imbalance:.2f})')

        # ── R-06: Gift Nifty ─────────────────────────────────────────────────
        gift_prem = snapshot.get('gift_nifty_premium')
        if gift_prem is not None:
            if is_gap_up and gift_prem > 0.5:
                score += 8
                log.append(f'R-06: Gift Nifty +{gift_prem:.2f}% supports gap up')
            elif is_gap_up and gift_prem <= 0:
                score -= 8
                is_fade_scenario = True
            elif not is_gap_up and gift_prem < -0.5:
                score += 8
                log.append(f'R-06: Gift Nifty {gift_prem:.2f}% supports gap down')
            elif not is_gap_up and gift_prem > 0:
                score -= 8
                is_fade_scenario = True

        # ── R-08: VIX Level ──────────────────────────────────────────────────
        if 18 <= vix <= 25:
            if gap_tier < 2 or catalyst_score < 7:
                multiplier *= 0.7
                log.append('R-08: Elevated VIX dampens score')
        elif vix < 13:
            multiplier *= 0.9

        # ── LAYER 3 R-11/R-12: Opening Range ─────────────────────────────────
        or_high = snapshot.get('or_high') or (snapshot.get('ohlc') or {}).get('high', 0)
        or_low  = snapshot.get('or_low')  or (snapshot.get('ohlc') or {}).get('low', 0)
        if or_high and or_low and last_close:
            if is_gap_up:
                if last_close > or_high:
                    score += 15; log.append('R-11: Price > OR_High (bullish continuation)')
                    confirmation_strong = True
                elif last_close < or_low:
                    score -= 15; is_fade_scenario = True; log.append('R-11: Price < OR_Low (gap failure)')
            else:
                if last_close < or_low:
                    score += 15; log.append('R-12: Price < OR_Low (bearish continuation)')
                    confirmation_strong = True
                elif last_close > or_high:
                    score -= 15; is_fade_scenario = True; log.append('R-12: Price > OR_High (V-shape recovery, gap fills)')

        # ── R-13: VWAP Position ───────────────────────────────────────────────
        if vwap_val and vwap_val > 0:
            vwap_diff = (last_close - vwap_val) / vwap_val * 100
            if is_gap_up:
                if last_close > vwap_val:
                    score += 8; log.append(f'R-13: Price > VWAP ({vwap_diff:.2f}% above)')
                elif vwap_diff < -1:
                    score -= 8; is_fade_scenario = True
                    log.append(f'R-13: Price rejected at VWAP ({vwap_diff:.2f}% below)')
            else:
                if last_close < vwap_val:
                    score += 8; log.append(f'R-13: Price < VWAP ({vwap_diff:.2f}% below)')
                elif vwap_diff > 1:
                    score -= 8; is_fade_scenario = True
                    log.append(f'R-13: Price rejected at VWAP ({vwap_diff:.2f}% above)')

        # ── LAYER 4 R-15: Sector Breadth ─────────────────────────────────────
        breadth = snapshot.get('sector_breadth_pct')
        if breadth is not None:
            if is_gap_up:
                if breadth > 70:
                    score += 10; log.append(f'R-15: Sector breadth {breadth}% > 70% (aligned)')
                elif breadth < 40:
                    score -= 10; is_fade_scenario = True
                    log.append(f'R-15: Sector breadth {breadth}% < 40% (divergence)')
            else:
                if breadth < 30:
                    score += 10; log.append(f'R-15: Sector breadth {breadth}% < 30% (aligned)')
                elif breadth > 60:
                    score -= 10; is_fade_scenario = True
                    log.append(f'R-15: Sector breadth {breadth}% > 60% (divergence)')

        # ── R-17: Volume Confirmation ─────────────────────────────────────────
        if volumes and len(volumes) >= 20:
            vol_ratio = compute_volume_ratio(volumes)
            if vol_ratio > 2:
                score += 8; log.append(f'R-17: High volume ({vol_ratio:.1f}x) confirms gap')
            elif vol_ratio < 0.5:
                score -= 8; is_fade_scenario = True
                log.append('R-17: Low volume (weak gap, fade risk)')

        # ── R-18: Holding OR Level ────────────────────────────────────────────
        if or_high and or_low:
            if is_gap_up:
                if last_close > or_high:
                    score += 5; log.append('R-18: Holding above OR_High')
                elif last_close < or_low:
                    score -= 5; log.append('R-18: Failed to hold OR level')
            else:
                if last_close < or_low:
                    score += 5; log.append('R-18: Holding below OR_Low')
                elif last_close > or_high:
                    score -= 5; log.append('R-18: Failed to hold OR level')

        # ── LAYER 5 R-20: Weekly Expiry + Banking ────────────────────────────
        if snapshot.get('is_weekly_expiry') and snapshot.get('sector') == 'Banking':
            multiplier *= 0.7; log.append('R-20: Weekly Expiry + Banking (0.7× multiplier)')

        # ── R-21: Index Divergence ────────────────────────────────────────────
        index_dir = snapshot.get('index_direction')
        if index_dir and index_dir != ('UP' if is_gap_up else 'DOWN'):
            score -= 6; is_fade_scenario = True
            log.append(f'R-21: Index divergence (stock {"up" if is_gap_up else "down"}, index opposite)')

        # ── LAYER 6 Late: Conditional Overrides ──────────────────────────────
        if snapshot.get('promoter_pledge_invoked') and not is_gap_up and score < -10:
            override_trigger = 'STRONG SELL'
            log.append('OV-05: Promoter pledge + gap down + negative momentum')

        earn_surp = snapshot.get('earnings_surprise_pct')
        if earn_surp is not None and earn_surp < -5 and is_gap_up and last_close < vwap_val:
            override_trigger = 'STRONG FADE'; is_fade_scenario = True
            log.append('OV-06: Earnings miss + gap up + VWAP break')

        if gap_tier >= 4 and abs_gap > 5 and highs and lows:
            price_move = ((today_open - lows[-1]) / today_open * 100) if is_gap_up \
                         else ((highs[-1] - today_open) / today_open * 100)
            if price_move > abs_gap * 0.5:
                override_trigger = 'FADE'; is_fade_scenario = True
                log.append(f'OV-07: Extreme gap ({abs_gap:.2f}%) + immediate reversal detected')

        final_score = score * multiplier

        return {
            'gapTier':           gap_tier,
            'score':             final_score,
            'override':          override_trigger,
            'log':               log,
            'gapType':           gap_type,
            'gapFillRate':       gap_history['fillRate'] if gap_history else None,
            'confirmationStrong': confirmation_strong,
            'isFadeScenario':    is_fade_scenario,
        }


# Singleton instance (mirrors JS: globalThis.gapAnalysisEngine = new GapAnalysisEngine())
gap_analysis_engine = GapAnalysisEngine()


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper — used by scoring_engine.py AND entry_validator.py
# Centralises the dynamic gap weighting logic so threshold changes need
# only one edit.
# ─────────────────────────────────────────────────────────────────────────────

def compute_gap_weighted_total(total: float, gap_res: dict) -> tuple:
    """Apply dynamic gap weighting to a score total.

    Args:
        total:   Current score (0-100).
        gap_res: Result dict from gap_analysis_engine.compute_gap_score().

    Returns:
        (new_total, overlay_factor_dict | None)
        overlay_factor_dict is ready to insert into a factors dict.
    """
    if not gap_res or gap_res.get('gapTier', 0) < 1:
        return total, None

    # Dynamic weight — Tier 3+ with strong confirmation → higher gap weight
    w_gap = 0.5 if (gap_res['gapTier'] >= 3 and gap_res.get('confirmationStrong')) else 0.4
    if gap_res.get('isFadeScenario'):
        w_gap = 0.3
    w_core = 1.0 - w_gap

    new_total = total * w_core + gap_res.get('score', 0) * w_gap

    label = (
        f"Gap [{gap_res['override']}]" if gap_res.get('override')
        else f"Gap Tier {gap_res['gapTier']} ({gap_res.get('gapType', '')})"
    )
    overlay = {
        'score':  min(max(gap_res.get('score', 0), -100), 100),
        'max':    100,
        'noData': False,
        'color':  '#D32F2F' if gap_res.get('override') else '#FF9800',
        'label':  label,
    }
    return new_total, overlay
