"""
scoring_engine.py — Python port of ScoringEngine (scoring-engine.js)
1-to-1 parity with JS version. Delegates all indicator math to indicators.py.

Equity Scoring  (5-factor): Technical(30) + PriceAction(25) + Volume(15) + MarketContext(20) + SectorMomentum(10) = 100
Options Scoring (6-factor): MomentumTrend(25) + VolumeOrderFlow(20) + Derivatives(20) + OptionsStructure(15) + MarketContext(15) + Catalyst(5) = 100

renderBreakdown / renderRiskRow are UI-only — intentionally omitted (frontend renders HTML).
"""
from session_utils import now_ist, is_late_session, get_session_mode, ist_timestamp
from indicators import (
    compute_ema, compute_rsi, compute_macd, compute_adx, compute_atr,
    compute_bollinger_width, compute_bollinger_bands,
    compute_vwap, compute_intraday_vwap, compute_volume_ratio,
    compute_risk_levels, compute_smc_bias,
)
from gap_analysis_engine import gap_analysis_engine, compute_gap_weighted_total
from constants import (
    SL_ATR_MULT, T1_ATR_MULT, T2_ATR_MULT,
    ADX_STRONG, ADX_MODERATE,
    RSI_CALL_MIN, RSI_CALL_MAX, RSI_PUT_MIN, RSI_PUT_MAX,
    VOL_STRONG, VOL_MODERATE,
    VWAP_SWEET_ZONE_PCT, VWAP_MAX_DEVIATION,
    IV_MAX_FOR_BUY, IV_CHEAP_MAX,
    OPTIONS_SIGNAL_STRONG, OPTIONS_SIGNAL_NORMAL, OPTIONS_NO_TRADE_MAX,
    EQUITY_SIGNAL_MIN,
    CIRCUIT_PROXIMITY_PCT, BID_ASK_MAX_SPREAD_PCT,
    SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE,
)


class ScoringEngine:

    def __init__(self):
        self.equity_weights = {
            'technical': 30, 'priceAction': 25, 'volume': 15,
            'marketContext': 20, 'sectorMomentum': 10,
        }
        self.options_weights = {
            'momentumTrend': 25, 'volumeOrderFlow': 20, 'derivatives': 20,
            'optionsStructure': 15, 'marketContext': 15, 'catalyst': 5,
        }
        # Populated by score_batch() after a full scan
        self._sector_scores = {}

    # ─────────────────────────────────────────────────────────────
    # EQUITY SCORING (5-Factor)
    # ─────────────────────────────────────────────────────────────

    def score_equity(self, data):
        closes   = data.get('closes', [])
        highs    = data.get('highs', [])
        lows     = data.get('lows', [])
        volumes  = data.get('volumes', [])
        sector_data = data.get('sectorData', {}) or {}

        if len(closes) < 2:
            return self._empty_result()

        rsi      = compute_rsi(closes)
        macd     = compute_macd(closes)
        adx      = compute_adx(highs, lows, closes)
        atr      = compute_atr(highs, lows, closes)
        ema9     = compute_ema(closes, 9)
        ema21    = compute_ema(closes, 21)
        ema50    = compute_ema(closes, 50)
        vol_ratio = compute_volume_ratio(volumes)

        last_close  = closes[-1]
        prev_close  = closes[-2] if len(closes) >= 2 else last_close
        ema9_last   = (ema9[-1]  if ema9  else None) or last_close
        ema21_last  = (ema21[-1] if ema21 else None) or last_close
        ema50_last  = (ema50[-1] if ema50 else None) or last_close
        change_pct  = (last_close - prev_close) / prev_close * 100

        is_bullish = last_close > ema21_last
        factors = {}

        # Compute technical consensus
        consensus_res = {'consensus': 'NEUTRAL', 'bullish': 0, 'bearish': 0, 'neutral': 0}
        if len(closes) >= 28:
            try:
                ohlcv_list = [{'close': closes[i], 'high': highs[i], 'low': lows[i], 'volume': volumes[i]} for i in range(len(closes))]
                from indicators import compute_technical_consensus
                consensus_res = compute_technical_consensus(ohlcv_list)
            except Exception:
                pass

        bull_votes = consensus_res['bullish']
        bear_votes = consensus_res['bearish']
        total_votes = bull_votes + bear_votes + consensus_res['neutral']
        total_votes = total_votes if total_votes > 0 else 23

        # ── 1. TECHNICAL MOMENTUM (30pts) ──
        tech = 0
        if is_bullish:
            if last_close > ema9_last and ema9_last > ema21_last: tech += 5
            elif last_close > ema21_last and ema9_last > ema21_last: tech += 3
            elif last_close > ema21_last: tech += 1
            if ema21_last > ema50_last: tech += 5
            elif abs(ema21_last - ema50_last) < ema50_last * 0.01: tech += 2
            if macd['histogram'] > 0: tech += 8
            elif macd['histogram'] > -0.0001: tech += 2
            if 45 <= rsi <= 65: tech += 7
            elif 40 <= rsi <= 70: tech += 4
            elif 30 <= rsi <= 75: tech += 1
        else:
            if last_close < ema9_last and ema9_last < ema21_last: tech += 5
            elif last_close < ema21_last and ema9_last < ema21_last: tech += 3
            elif last_close < ema21_last: tech += 1
            if ema21_last < ema50_last: tech += 5
            elif abs(ema21_last - ema50_last) < ema50_last * 0.01: tech += 2
            if macd['histogram'] < 0: tech += 8
            elif macd['histogram'] < 0.0001: tech += 2
            if 35 <= rsi <= 50: tech += 7
            elif rsi > 60 and rsi <= 75: tech += 3
            elif 30 <= rsi <= 60: tech += 4
        if adx > 25: tech += 5
        elif adx > 20: tech += 3
        factors['technical'] = {'score': min(tech, 30), 'max': 30, 'label': 'Technical Momentum', 'color': '#1E88E5'}

        # ── 2. PRICE ACTION (25pts) ──
        pa = 0
        high20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        low20  = min(lows[-20:])  if len(lows)  >= 20 else min(lows)
        range_ = high20 - low20
        pos_in_range = ((last_close - low20) / range_) if range_ > 0 else 0.5
        if is_bullish:
            if last_close >= high20 * 0.98: pa += 10
            elif last_close >= high20 * 0.95: pa += 7
            elif last_close >= high20 * 0.90: pa += 4
            if vol_ratio > 2: pa += 8
            elif vol_ratio > 1.5: pa += 5
            elif vol_ratio > 1.2: pa += 2
            if pos_in_range > 0.7: pa += 7
            elif pos_in_range > 0.6: pa += 4
            elif pos_in_range > 0.5: pa += 2
        else:
            if last_close <= low20 * 1.02: pa += 10
            elif last_close <= low20 * 1.05: pa += 7
            elif last_close <= low20 * 1.10: pa += 4
            if change_pct < -0.5: pa += 5
            elif change_pct < 0: pa += 2
            if len(volumes) >= 2:
                prev2 = closes[-3] if len(closes) >= 3 else prev_close
                if prev2 > ema21_last and last_close < prev2: pa += 5
                elif last_close < ema21_last: pa += 2
            if pos_in_range < 0.3: pa += 5
            elif pos_in_range < 0.4: pa += 3
            elif pos_in_range < 0.5: pa += 1
        factors['priceAction'] = {'score': min(pa, 25), 'max': 25, 'label': 'Price Action', 'color': '#00BCD4'}

        # ── 3. VOLUME & DISTRIBUTION (15pts) ──
        vol = 0
        if vol_ratio > 2: vol += 6
        elif vol_ratio > 1.5: vol += 4
        elif vol_ratio > 1.0: vol += 2
        if len(volumes) >= 2:
            today_vol = volumes[-1]; prev_vol = volumes[-2]
            if is_bullish:
                if last_close > prev_close and today_vol > prev_vol * 1.2: vol += 5
                elif last_close > prev_close and today_vol > prev_vol: vol += 3
            else:
                if last_close < prev_close and today_vol > prev_vol * 1.2: vol += 5
                elif last_close < prev_close and today_vol > prev_vol: vol += 3
        if len(volumes) >= 5:
            recent = sum(volumes[-3:]) / 3
            prior  = sum(volumes[-5:-3]) / 2
            if is_bullish:
                if recent > prior * 1.3 and change_pct < 1: vol += 4
                elif recent > prior * 1.1: vol += 2
            else:
                if recent > prior * 1.3 and change_pct < -0.5: vol += 4
                elif recent > prior * 1.1 and change_pct < 0: vol += 2
        factors['volume'] = {'score': min(vol, 15), 'max': 15, 'label': 'Volume & Distribution', 'color': '#AB47BC'}

        # ── 4. MARKET CONTEXT (20pts) ──
        mc = 0
        if adx > 20: mc += 8
        elif adx > 15: mc += 4
        if adx > 20: mc += 6
        elif adx > 15: mc += 3
        sector_name = data.get('sector', '')
        auto_sector = self._sector_scores.get(sector_name) if sector_name else None
        merged_sector = auto_sector or sector_data
        if merged_sector and merged_sector.get('relativeStrength') is not None:
            ss = merged_sector.get('relativeStrength', 50)
            if is_bullish:
                if ss > 70: mc += 6
                elif ss > 60: mc += 3
            else:
                if ss < 30: mc += 6
                elif ss < 40: mc += 3
        factors['marketContext'] = {'score': min(mc, 20), 'max': 20, 'label': 'Market Context', 'color': '#26A69A'}

        # ── 5. SECTOR MOMENTUM (10pts) ──
        sec = 0; has_real_sector = False
        auto_sector2 = self._sector_scores.get(sector_name) if sector_name else None
        merged2 = auto_sector2 or sector_data
        if merged2 and (merged2.get('relativeStrength') is not None or merged2.get('rotating') is not None):
            has_real_sector = True
            ss2 = merged2.get('relativeStrength', 50)
            if is_bullish:
                if ss2 > 70: sec += 7
                elif ss2 > 60: sec += 4
                elif ss2 > 50: sec += 2
            else:
                if ss2 < 30: sec += 7
                elif ss2 < 40: sec += 4
                elif ss2 < 50: sec += 2
            if merged2.get('rotating'): sec += 3
        factors['sectorMomentum'] = {'score': min(sec, 10), 'max': 10, 'label': 'Sector Momentum',
                                     'color': '#FFA726', 'noData': not has_real_sector}

        # ── Total + normalization ──
        total = sum(f['score'] for f in factors.values())
        available_max = sum(f['max'] for f in factors.values() if not f.get('noData'))
        if 0 < available_max < 100:
            total = round(total / available_max * 100)

        # ── Mandatory PUT filters ──
        put_veto = False; put_reasons = []
        if not is_bullish:
            if adx > 25 and ema21_last > ema50_last:
                put_veto = True; put_reasons.append('Index strong uptrend (ADX>25 + EMA21>EMA50) = trap risk')
            if change_pct < -3.5:
                put_veto = True; put_reasons.append(f'Stock already {change_pct:.1f}% down (>3-4%) = late entry risk')
            if factors['priceAction']['score'] < 5:
                put_veto = True; put_reasons.append('No clear breakdown + pullback + rejection pattern')

        if not is_bullish and put_veto:
            direction = 'NO TRADE'; total = min(total, 39)
            factors['putFilter'] = {'score': 0, 'max': 100, 'noData': False, 'color': '#D32F2F',
                                    'label': f"⚠ PUT Filter: {' | '.join(put_reasons)}"}
        else:
            if is_bullish and total >= 55: direction = 'BULLISH'
            elif not is_bullish and total >= 55: direction = 'BEARISH'
            else: direction = 'NEUTRAL'

        # ── Gap overlay ──
        try:
            gap_res = gap_analysis_engine.compute_gap_score(data)
            new_total, overlay = compute_gap_weighted_total(total, gap_res)
            if overlay is not None:
                total = new_total
                factors['gapOverlay'] = overlay
                if gap_res.get('override') and gap_res.get('confirmationStrong'):
                    direction = gap_res['override']
        except Exception:
            pass

        # ── Risk management ──
        entry = data.get('ltp', last_close) or last_close
        is_long = is_bullish or direction == 'BULLISH'
        risk = compute_risk_levels(entry, atr, is_long)

        # ── HTF SMC bias overlay (informational only — does NOT affect total) ──
        # Call site passes 'dailyOhlcv' when available (from /api/stock-analysis).
        # If absent, smcBias is omitted from the return to keep response lean.
        smc_bias = None
        daily_ohlcv = data.get('dailyOhlcv') or data.get('daily_ohlcv')
        if daily_ohlcv and len(daily_ohlcv) >= 20:
            try:
                smc_bias = compute_smc_bias(daily_ohlcv,
                                            current_ltp=entry, current_atr=atr)
            except Exception:
                pass

        result = {
            'total': round(total), 'factors': factors, 'direction': direction,
            'rsi': rsi, 'macd': macd, 'adx': adx, 'atr': atr, 'volRatio': vol_ratio,
            'changePercent': change_pct, 'isBullishTrend': is_bullish,
            'putFilterVeto': put_veto, 'putFilterReasons': put_reasons,
            'risk': risk,
            'technicalConsensus': consensus_res,
        }
        if smc_bias is not None:
            result['smcBias'] = smc_bias
        return result

    # ─────────────────────────────────────────────────────────────
    # OPTIONS SCORING (6-Factor)
    # ─────────────────────────────────────────────────────────────

    def score_options(self, data):
        closes       = data.get('closes', [])
        highs        = data.get('highs', [])
        lows         = data.get('lows', [])
        volumes      = data.get('volumes', [])
        options_data = data.get('optionsData', {}) or {}
        fundamentals = data.get('fundamentals', {}) or {}
        snapshot     = data.get('snapshot', {}) or {}

        if len(closes) < 2:
            return self._empty_result()

        last_close = closes[-1]; prev_close = closes[-2] if len(closes) >= 2 else last_close
        rsi  = compute_rsi(closes);  macd = compute_macd(closes)
        adx  = compute_adx(highs, lows, closes)
        atr  = compute_atr(highs, lows, closes)
        vol_ratio = compute_volume_ratio(volumes)
        ema9  = compute_ema(closes, 9)
        ema21 = compute_ema(closes, 21)
        ema50 = compute_ema(closes, 50)

        # ── Session mode — IST-aware, passed in or auto-detected ──
        session_mode = data.get('session_mode') or get_session_mode()
        is_live = (session_mode == 'live')
        is_premarket_mode = (session_mode == 'premarket')

        live_change_pct = snapshot.get('change_pct', (last_close - prev_close) / prev_close * 100)
        live_buy_qty    = snapshot.get('buy_qty', 0)
        live_sell_qty   = snapshot.get('sell_qty', 0)
        live_volume     = snapshot.get('volume', volumes[-1] if volumes else 0)
        # Gap 1 fix: treat missing VWAP as risk-filter trigger during live session
        _raw_vwap = snapshot.get('avg_price') or snapshot.get('vwap')
        vwap_missing = (_raw_vwap is None or _raw_vwap == 0) and is_live
        vwap            = _raw_vwap or last_close   # fallback for scoring arithmetic only
        live_ltp        = snapshot.get('ltp', last_close) or last_close
        circuit         = snapshot.get('circuit', {}) or {}
        depth           = snapshot.get('depth', {}) or {}
        futures         = snapshot.get('futures', {}) or {}
        atm_opt         = snapshot.get('atm_option', {}) or {}

        ema9_last  = (ema9[-1]  if ema9  else None) or last_close
        ema21_last = (ema21[-1] if ema21 else None) or last_close
        ema50_last = (ema50[-1] if ema50 else None) or last_close

        is_call_bias = ema21_last > ema50_last
        factors = {}

        # Compute technical consensus
        consensus_res = {'consensus': 'NEUTRAL', 'bullish': 0, 'bearish': 0, 'neutral': 0}
        if len(closes) >= 28:
            try:
                ohlcv_list = [{'close': closes[i], 'high': highs[i], 'low': lows[i], 'volume': volumes[i]} for i in range(len(closes))]
                from indicators import compute_technical_consensus
                consensus_res = compute_technical_consensus(ohlcv_list)
            except Exception:
                pass

        bull_votes = consensus_res['bullish']
        bear_votes = consensus_res['bearish']
        total_votes = bull_votes + bear_votes + consensus_res['neutral']
        total_votes = total_votes if total_votes > 0 else 23

        # ── Global risk filters ──
        risk_veto = False; risk_reasons = []

        # Gap 1: Missing live VWAP → cannot assess deviation → veto
        if vwap_missing:
            risk_veto = True
            risk_reasons.append('Live VWAP unavailable from broker — cannot assess price deviation')

        price_vwap_pct = ((live_ltp - vwap) / vwap * 100) if vwap > 0 else 0
        if not vwap_missing and abs(price_vwap_pct) > VWAP_MAX_DEVIATION:
            risk_veto = True; risk_reasons.append(f'Price deviation from VWAP {price_vwap_pct:.1f}% > ±{VWAP_MAX_DEVIATION}%')
        iv = atm_opt.get('avg_iv') or options_data.get('ivPercentile', 0)
        if iv and iv > IV_MAX_FOR_BUY:
            risk_veto = True; risk_reasons.append(f'IV {iv:.1f}% > {IV_MAX_FOR_BUY}%')
        if circuit.get('upper') and circuit.get('lower') and last_close > 0:
            upper_dist = (circuit['upper'] - last_close) / last_close * 100
            lower_dist = (last_close - circuit['lower']) / last_close * 100
            if upper_dist < CIRCUIT_PROXIMITY_PCT or lower_dist < CIRCUIT_PROXIMITY_PCT:
                risk_veto = True; risk_reasons.append(f'Circuit proximity: upper {upper_dist:.1f}%, lower {lower_dist:.1f}%')
        if adx < ADX_MODERATE:
            risk_veto = True; risk_reasons.append(f'ADX {adx:.1f} < {ADX_MODERATE} (low trend strength)')

        # Gap 3 fix: Use IST-aware session mode instead of naive datetime.now()
        # session_mode is set at top of this method (live/premarket/historical)
        if is_live and is_late_session():
            risk_veto = True
            n = now_ist()
            risk_reasons.append(f'After {SESSION_CLOSE_HOUR}:{SESSION_CLOSE_MINUTE:02d} PM IST ({n.hour}:{n.minute:02d} IST)')

        if depth.get('bid') and depth.get('ask') and depth['bid'] > 0:
            spread = (depth['ask'] - depth['bid']) / depth['bid'] * 100
            if spread > BID_ASK_MAX_SPREAD_PCT:
                risk_veto = True; risk_reasons.append(f'Bid-ask spread {spread:.2f}% > {BID_ASK_MAX_SPREAD_PCT}%')

        # ── 1. MOMENTUM + TREND (25pts) ──
        # VWAP is only a meaningful intraday anchor for index underlyings
        _sym_upper = (data.get('symbol') or '').upper()
        _INDEX_SYMS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
        is_index_underlying = any(idx in _sym_upper for idx in _INDEX_SYMS)
        mt = 0
        if is_call_bias:
            if is_index_underlying:
                # Indices: VWAP is the primary intraday anchor (original logic)
                if live_ltp > vwap: mt += 8
                elif live_ltp > vwap * 0.99: mt += 4
                pct_above = (live_ltp - vwap) / vwap * 100 if vwap > 0 else 0
                if 0 < pct_above <= 1.5: mt += 5
                elif 0 < pct_above <= 2.5: mt += 3
                elif pct_above > 0: mt += 1
                if ema9_last > ema21_last and live_ltp > ema9_last: mt += 6
                elif ema9_last > ema21_last and live_ltp > ema21_last: mt += 4
                elif ema9_last > ema21_last: mt += 2
                if macd['histogram'] > 0: mt += 3
                elif abs(macd['histogram']) < 0.00001: mt += 1
                if 45 <= rsi <= 65: mt += 3
                elif 40 <= rsi <= 70: mt += 2
                elif 30 <= rsi <= 75: mt += 1
            else:
                # Stock options: no VWAP — redistribute 13pts into EMA/MACD/RSI
                if ema9_last > ema21_last and live_ltp > ema9_last: mt += 11
                elif ema9_last > ema21_last and live_ltp > ema21_last: mt += 8
                elif ema9_last > ema21_last: mt += 4
                if macd['histogram'] > 0: mt += 8
                elif abs(macd['histogram']) < 0.00001: mt += 3
                if 45 <= rsi <= 65: mt += 6
                elif 40 <= rsi <= 70: mt += 4
                elif 30 <= rsi <= 75: mt += 2
        else:
            if is_index_underlying:
                if live_ltp < vwap: mt += 8
                elif live_ltp < vwap * 1.01: mt += 4
                pct_below = (vwap - live_ltp) / vwap * 100 if vwap > 0 else 0
                if 0 < pct_below <= 1.5: mt += 5
                elif 0 < pct_below <= 2.5: mt += 3
                elif pct_below > 0: mt += 1
                if ema9_last < ema21_last and live_ltp < ema9_last: mt += 6
                elif ema9_last < ema21_last and live_ltp < ema21_last: mt += 4
                elif ema9_last < ema21_last: mt += 2
                if macd['histogram'] < 0: mt += 3
                elif abs(macd['histogram']) < 0.00001: mt += 1
                if 35 <= rsi <= 55: mt += 3
                elif 30 <= rsi <= 60: mt += 2
                elif 25 <= rsi <= 70: mt += 1
            else:
                if ema9_last < ema21_last and live_ltp < ema9_last: mt += 11
                elif ema9_last < ema21_last and live_ltp < ema21_last: mt += 8
                elif ema9_last < ema21_last: mt += 4
                if macd['histogram'] < 0: mt += 8
                elif abs(macd['histogram']) < 0.00001: mt += 3
                if 35 <= rsi <= 55: mt += 6
                elif 30 <= rsi <= 60: mt += 4
                elif 25 <= rsi <= 70: mt += 2
        factors['momentumTrend'] = {'score': min(mt, 25), 'max': 25, 'label': 'Momentum + Trend', 'color': '#FF5722'}

        # ── 2. VOLUME & ORDER FLOW (20pts) ──
        vof = 0
        if vol_ratio >= 2.5: vof += 8
        elif vol_ratio >= 2.0: vof += 5
        elif vol_ratio >= 1.5: vof += 3
        elif vol_ratio >= 1.0: vof += 1
        if len(volumes) >= 3:
            avg_prev2 = sum(volumes[-3:-1]) / 2; cur_vol = volumes[-1]
            if cur_vol > avg_prev2 * 1.5: vof += 5
            elif cur_vol > avg_prev2 * 1.2: vof += 3
            elif cur_vol > avg_prev2: vof += 1
        # Gap 4 fix: buy/sell qty from pre-open book is unreliable — only use during live session
        total_qty = live_buy_qty + live_sell_qty
        if is_live and total_qty > 0:
            buy_ratio = live_buy_qty / total_qty
            if is_call_bias:
                if buy_ratio > 0.60: vof += 4
                elif buy_ratio > 0.55: vof += 2
                elif buy_ratio > 0.50: vof += 1
            else:
                if buy_ratio < 0.40: vof += 4
                elif buy_ratio < 0.45: vof += 2
                elif buy_ratio < 0.50: vof += 1
        elif is_premarket_mode and total_qty > 0:
            # Pre-open book exists but is unreliable — exclude from score, flag it
            pass  # noData handled in factor label below
        max_bid = depth.get('max_bid_qty', 0); max_ask = depth.get('max_ask_qty', 0)
        avg_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else (live_volume or 1)
        block_thresh = avg_vol * 0.01
        if max_bid > block_thresh or max_ask > block_thresh: vof += 3
        elif max_bid > block_thresh * 0.5 or max_ask > block_thresh * 0.5: vof += 1
        flow_label = 'Volume & Order Flow (pre-open, order flow excluded)' if is_premarket_mode else 'Volume & Order Flow'
        factors['volumeOrderFlow'] = {'score': min(vof, 20), 'max': 20, 'label': flow_label, 'color': '#9C27B0'}

        # ── 3. DERIVATIVES (OI + Futures) (20pts) ──
        # Gap 2 fix: if optionsData empty (not fetched from broker), mark noData
        # so the factor is excluded from the denominator during normalization.
        der = 0
        oi_change  = options_data.get('oiChangePercent', 0)
        oi_buildup = options_data.get('buildUp', 'none')
        fut_prem   = futures.get('premium_pct', 0)
        has_oi_data = bool(options_data and (options_data.get('oiChangePercent') is not None
                                             or options_data.get('buildUp')))
        has_futures = bool(futures and futures.get('premium_pct') is not None)
        der_no_data = not has_oi_data and not has_futures
        if not der_no_data:
            if is_call_bias:
                if oi_change > 5 and live_change_pct > 0.3: der += 8
                elif oi_change > 2 and live_change_pct > 0: der += 5
                elif oi_change > 0 and live_change_pct > 0: der += 2
                if oi_buildup == 'long_buildup': der += 5
                elif oi_buildup == 'short_covering': der += 2
                if fut_prem > 0.3: der += 4
                elif fut_prem > 0.1: der += 2
                elif fut_prem > 0: der += 1
                if abs(oi_change) < 2: der += 3
                elif abs(oi_change) < 5: der += 1
            else:
                if oi_change > 5 and live_change_pct < -0.3: der += 8
                elif oi_change > 2 and live_change_pct < 0: der += 5
                elif oi_change > 0 and live_change_pct < 0: der += 2
                if oi_buildup == 'short_buildup': der += 5
                elif oi_buildup == 'long_unwinding': der += 2
                if fut_prem < -0.3: der += 4
                elif fut_prem < -0.1: der += 2
                elif fut_prem < 0: der += 1
                if abs(oi_change) < 2: der += 3
                elif abs(oi_change) < 5: der += 1
        factors['derivatives'] = {
            'score': min(der, 20), 'max': 20,
            'label': 'Derivatives (OI + Futures)' + (' — no live data' if der_no_data else ''),
            'color': '#EF5350', 'noData': der_no_data,
        }

        # ── 4. OPTIONS STRUCTURE (15pts) ──
        # Gap 2 fix: mark noData when no options chain data available
        os_ = 0
        pcr  = atm_opt.get('pcr') or options_data.get('pcr', 0)
        ce_iv = atm_opt.get('ce_iv', 0); pe_iv = atm_opt.get('pe_iv', 0)
        has_options_chain = bool(pcr or iv or ce_iv or pe_iv)
        if has_options_chain:
            if iv and iv > 0:
                if iv < IV_CHEAP_MAX: os_ += 6
                elif iv < 50: os_ += 3
            if is_call_bias:
                if pcr > 1.3: os_ += 5
                elif pcr > 1.1: os_ += 3
                elif pcr > 0.9: os_ += 1
                if pe_iv > 0 and ce_iv > 0:
                    skew = pe_iv - ce_iv
                    if skew > 3: os_ += 4
                    elif skew > 1: os_ += 2
            else:
                if pcr < 0.7: os_ += 5
                elif pcr < 0.9: os_ += 3
                elif pcr < 1.1: os_ += 1
                if ce_iv > 0 and pe_iv > 0:
                    skew = ce_iv - pe_iv
                    if skew > 3: os_ += 4
                    elif skew > 1: os_ += 2
        factors['optionsStructure'] = {
            'score': min(os_, 15), 'max': 15,
            'label': 'Options Structure' + (' — no live data' if not has_options_chain else ''),
            'color': '#AB47BC', 'noData': not has_options_chain,
        }

        # ── 5. MARKET CONTEXT (15pts) ──
        mc = 3  # trend proxy baseline
        if adx > 20: mc += 5
        elif adx > 15: mc += 2
        delivery_pct = fundamentals.get('deliveryPct', 0)
        if delivery_pct > 60: mc += 4
        elif delivery_pct > 50: mc += 2
        elif delivery_pct > 40: mc += 1
        factors['marketContext'] = {'score': min(mc, 15), 'max': 15, 'label': 'Market Context', 'color': '#26A69A'}

        # ── 6. CATALYST BONUS (5pts) ──
        cat = 0
        if adx > 25: cat += 2
        elif adx > 20: cat += 1
        if delivery_pct > 60: cat += 1
        if is_call_bias:
            if pcr > 1.5: cat += 2
            elif pcr > 1.3: cat += 1
        else:
            if pcr < 0.5: cat += 2
            elif pcr < 0.7: cat += 1
        factors['catalyst'] = {'score': min(cat, 5), 'max': 5, 'label': 'Catalyst Bonus', 'color': '#1E88E5'}

        # ── Total + normalization ──
        total = sum(f['score'] for f in factors.values())
        available_max = sum(f['max'] for f in factors.values() if not f.get('noData'))
        if 0 < available_max < 100:
            total = round(total / available_max * 100)

        # ── Signal direction with 4-tier thresholds ──
        direction = 'NEUTRAL'; signal_strength = 'NEUTRAL'
        if not risk_veto:
            if total >= 75:
                direction = 'CALL' if is_call_bias else 'PUT'; signal_strength = 'STRONG'
            elif total >= 60:
                direction = 'CALL' if is_call_bias else 'PUT'; signal_strength = 'NORMAL'
            elif total <= 40:
                direction = 'NO TRADE'; signal_strength = 'WEAK'

        if risk_veto:
            direction = 'NO TRADE'; signal_strength = 'VETO'; total = min(total, 39)
            factors['riskFilter'] = {'score': 0, 'max': 100, 'noData': False, 'color': '#D32F2F',
                                     'label': f"⚠ Risk Filter: {', '.join(risk_reasons)}"}

        # ── Gap overlay ──
        try:
            gap_res = gap_analysis_engine.compute_gap_score(data)
            new_total, overlay = compute_gap_weighted_total(total, gap_res)
            if overlay is not None:
                total = new_total
                factors['gapOverlay'] = overlay
                if gap_res.get('override') and gap_res.get('confirmationStrong'):
                    direction = gap_res['override']
        except Exception:
            pass

        # ── Risk management ──
        entry = data.get('ltp', last_close) or last_close
        is_long = is_call_bias or 'CALL' in direction
        risk = compute_risk_levels(entry, atr, is_long)

        # ── HTF SMC bias overlay (informational only — does NOT affect total) ──
        smc_bias = None
        daily_ohlcv = data.get('dailyOhlcv') or data.get('daily_ohlcv')
        if daily_ohlcv and len(daily_ohlcv) >= 20:
            try:
                smc_bias = compute_smc_bias(daily_ohlcv,
                                            current_ltp=entry, current_atr=atr)
            except Exception:
                pass

        result = {
            'total': round(total), 'factors': factors, 'direction': direction,
            'signalStrength': signal_strength, 'rsi': rsi, 'macd': macd,
            'adx': adx, 'atr': atr, 'volRatio': vol_ratio,
            'iv': iv or 0, 'pcr': pcr, 'futPremium': fut_prem,
            'oiBuildUp': oi_buildup, 'blockDeal': False,
            'liveChangePct': live_change_pct, 'isCallBias': is_call_bias,
            'riskFilterVeto': risk_veto, 'riskFilterReasons': risk_reasons,
            'risk': risk,
            'sessionMode': session_mode,
            'technicalConsensus': consensus_res,
        }
        if smc_bias is not None:
            result['smcBias'] = smc_bias
        return result

    # ─────────────────────────────────────────────────────────────
    # Batch scoring
    # ─────────────────────────────────────────────────────────────

    def score_batch(self, stock_data_list, mode='equity'):
        results = []
        for stock in stock_data_list:
            result = self.score_equity(stock) if mode == 'equity' else self.score_options(stock)
            results.append({
                'symbol': stock.get('symbol', ''),
                'sector': stock.get('sector', '—'),
                'ltp':    stock['closes'][-1] if stock.get('closes') else 0,
                **result,
            })

        # Compute sector relative strength
        if mode == 'equity':
            sector_groups = {}
            for r in results:
                s = r.get('sector', '')
                if not s or s == '—': continue
                sector_groups.setdefault(s, []).append(r['total'])
            all_scores = [r['total'] for r in results]
            overall_avg = sum(all_scores) / len(all_scores) if all_scores else 50
            for sector, scores in sector_groups.items():
                avg = sum(scores) / len(scores)
                rs  = min(100, max(0, 50 + (avg - overall_avg) * 2))
                self._sector_scores[sector] = {'relativeStrength': rs, 'rotating': avg > overall_avg + 5}

        return sorted(results, key=lambda r: r['total'], reverse=True)

    # ─────────────────────────────────────────────────────────────
    # Strike selection
    # ─────────────────────────────────────────────────────────────

    def select_optimal_strike(self, strike_data, underlying_price, atr, direction='CALL'):
        candidates = [
            s for s in strike_data
            if 0.35 <= abs(s.get('delta', 0)) <= 0.55
            and s.get('oi', 0) >= 5000
            and s.get('volume', 0) >= 500
            and (s.get('bidAskSpread', 0) / (s.get('ltp', 1) or 1)) < 0.05
        ]
        if not candidates:
            return None
        target_move = atr * 1.5
        best = None; best_er = 0
        for s in candidates:
            er = (target_move * abs(s.get('delta', 0.45))) / (s.get('ltp', 1) or 1)
            if er > best_er:
                best_er = er; best = {**s, '_expectedReturn': er}
        return best

    # ─────────────────────────────────────────────────────────────
    # Position sizing
    # ─────────────────────────────────────────────────────────────

    def compute_position_size(self, capital, risk_pct, entry_price, stop_loss):
        risk_amount   = capital * (risk_pct / 100)
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            return {'shares': 0, 'lotValue': 0, 'riskAmount': 0}
        shares = int(risk_amount / risk_per_share)
        return {
            'shares':      shares,
            'lotValue':    round(shares * entry_price),
            'riskAmount':  round(shares * risk_per_share),
            'riskPerShare': round(risk_per_share, 2),
        }

    # ─────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────

    def _empty_result(self):
        """Unified empty result — schema matches entry_validator._error_result()."""
        return {
            'isValid':        False,
            'direction':      'NO TRADE',
            'setupType':      'NONE',
            'total':          0,
            'factors':        {},
            'confidence':     0,
            'rsi':   50, 'macd': {'macd': 0, 'signal': 0, 'histogram': 0},
            'adx':    0, 'atr':  0, 'volRatio': 1,
            'changePercent':  0,
            'isBullishTrend': False,
            'putFilterVeto':  False,
            'putFilterReasons': [],
            'reasoning':      ['⚠ Insufficient data'],
            'risk': {'entry': 0, 'stopLoss': 0, 'target1': 0, 'target2': 0,
                     'riskReward': 0, 'riskPerShare': 0, 'atrUsed': 0},
        }


# Singleton — mirrors JS: globalThis.scoringEngine = new ScoringEngine()
scoring_engine = ScoringEngine()
