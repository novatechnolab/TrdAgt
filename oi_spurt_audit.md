# OI Spurt Scanner — Expert Audit Report
### Perspective: Indian Retail Trader / F&O Practitioner

---

## 🔴 CRITICAL — Causes Incorrect Information

### 1. `oi_day_high` used as "Previous OI" — **Wrong Proxy**
**File:** `oi_spurt_routes.py` · Line 167

```python
prev_oi = q.get("oi_day_high", oi)   # proxy for previous OI
oi_chg  = oi - prev_oi
```

**Problem:** `oi_day_high` is the intraday OI high-water mark, NOT the previous day's OI. This means:
- `oi_chg` in the option chain will frequently be **negative** even when OI is actually building up versus yesterday
- Buildup classification (`Long Buildup` / `Short Buildup`) derived from this will be **systematically wrong** for any strike where intraday OI hasn't exceeded yesterday's close
- The **Trap signals** downstream are therefore unreliable
- During morning session (9:15–10:00 AM), `oi_day_high ≈ current_oi` → `oi_chg ≈ 0` → all strikes show "Long Unwinding" incorrectly

**Correct field from Kite:** Use `oi_day_low` as previous or, better, fetch `historical` OI from NSE directly. The cleanest approach is to store previous day OI on first fetch each session.

**Fix:** Cache yesterday's OI per strike at session start, compare against it. Or use NSE's option chain API which provides `changeinOpenInterest` directly.

---

### 2. `price_change` Always Shows 0.0 — **Wrong Bias Signal**
**File:** `oi_spurt_routes.py` · Line 100

```python
"price_change": 0.0,   # not provided by NSE; enriched via Kite LTP later
```

**File:** `oi-spurt-scanner.html` · Line 386

```javascript
const priceChg = spurtRow.price_change || 0;
```

**Problem:** `price_change` is always 0 in `spurtData`. The right panel shows `↔ Neutral` for **all stocks** regardless of actual price direction. The LTP colour in the left panel also never changes from neutral. This is misleading when a stock has surged 5% alongside OI — a critical signal distinction (Long Buildup vs Short Covering).

**Fix:** In `fetch_oi_spurt()`, compute `price_change` using `underlyingValue` vs `previousClose` from NSE data, or enrich from Kite's `ltp()` call.

---

### 3. Max Pain Calculation Uses **Only Nearest Expiry**
**File:** `oi_spurt_routes.py` · Lines 145–146

```python
nearest = sorted(set(i["expiry"] for i in opts))[0]
chain_instr = [i for i in opts if i["expiry"] == nearest]
```

**Problem:** For weekly expiry stocks (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY), the **nearest expiry is current-week expiry**. Max Pain computed from only current-week chain is valid only on/near expiry. A retail trader looking on Monday needs to know whether the stock/index is being manipulated toward current-week or next-week max pain. No expiry selector is provided.

Additionally, for **stocks with monthly expiry only** (e.g., RELIANCE, TCS), this is fine. But if the current week's expiry has already passed and instruments list is cached (10-min TTL), you could be fetching from an **expired expiry's instruments** for up to 10 minutes post-expiry.

**Fix:** Add expiry selector (Weekly/Monthly). Invalidate instruments cache on expiry day at 15:30.

---

### 4. `nse_session()` — Stale Session Bug After 25 Minutes
**File:** `oi_spurt_routes.py` · Line 50

```python
if _nse_sess is not None and (now - _nse_sess_time).seconds <= 1500:
```

**Problem:** `.seconds` on a `timedelta` object returns only the **seconds component** (0–59), NOT total seconds. For a 30-minute-old session, `timedelta.seconds` returns ~0–59 (the seconds part of the time difference), not 1800. This means:
- The session is **never refreshed** — it will use the same session object for hours
- NSE cookies expire in ~15 minutes; after expiry, all NSE requests start returning 403/empty

**Fix:** Use `(now - _nse_sess_time).total_seconds()` instead of `.seconds`.

---

## 🟠 HIGH — Misleading or Incomplete Data

### 5. Buildup Classification: `price <= 0` is Wrong Condition
**File:** `oi_spurt_routes.py` · Line 231

```python
elif oi_chg > 0 and price <= 0: buildup = "Short Buildup"
```

**Problem:** `price` here is the option's LTP. An option LTP is **always ≥ 0** — it cannot be negative. A deep OTM option can have LTP of ₹0.05 which is effectively non-zero but the condition `price <= 0` would classify it as "Long Buildup" instead of the borderline OTM buildup it actually is. The real distinction should be:
- OI↑ + price↑ = Long Buildup (new longs)
- OI↑ + price↓ = Short Buildup (new writers)
- The check should be on **price change** (vs previous), not on price level

Also: price being `0` when Kite returns no quote (empty instrument) is treated as "Short Buildup" — completely wrong.

**Fix:** Use `q.get("last_price", None)` and check if price changed up or down vs previous close, not whether price > 0.

---

### 6. PCR Interpretation Thresholds are Incorrect for Stocks
**File:** `oi-spurt-scanner.html` · Lines 399–403

```javascript
if(pcr>=1.5){pcrTag='Extreme Bullish';}
else if(pcr>=1.2){pcrTag='Bullish';}
else if(pcr>=0.8){pcrTag='Neutral';}
else if(pcr>=0.5){pcrTag='Bearish';}
```

**Problem:** These thresholds are calibrated for **Index PCR** (NIFTY/BANKNIFTY). For individual F&O stocks:
- Stocks typically have PCR 0.3–0.7 due to concentrated hedging behaviour
- A stock PCR of 0.8 is actually **extreme bullish** for stocks
- A stock PCR of 1.5 is near-impossible outside major events

Showing "Bearish" for a stock with PCR=0.6 when that's actually normal/neutral for that stock misleads the trader.

**Fix:** Use different thresholds for stocks vs indices, or add a "Stock PCR norms" note in the UI. Alternatively show raw PCR + rank vs 20-day average PCR.

---

### 7. Pivot Levels are **Today's Intraday OHLC**, Not Previous Day's OHLC
**File:** `oi_spurt_routes.py` · Line 285–289

```python
ohlc = kite.ohlc([exch_sym])
d    = ohlc.get(exch_sym, {})
o    = d.get("ohlc", {})
pivots = compute_pivots(o["high"], o["low"], o["close"])
```

**Problem:** `kite.ohlc()` returns **today's** OHLC (intraday), not yesterday's closing OHLC. Pivot levels must be calculated from the **previous session's High, Low, Close**. Using today's intraday H/L/C makes pivots a moving target during the day — they shift every tick, which defeats their purpose as static reference levels.

**Fix:** Fetch previous day's OHLC from `kite.historical_data()` for the last trading session, or use NSE bhav copy. The pivot formula is: `P = (PrevH + PrevL + PrevC) / 3`.

---

### 8. "CE Wall" and "PE Wall" Show Highest OI Strike — Not Max OI Near ATM
**File:** `oi-spurt-scanner.html` · Lines 500–501

```javascript
top_ce[0]  // first element of top 5 CE by OI — could be far OTM
top_pe[0]  // first element of top 5 PE by OI
```

**Problem:** `top_ce` is sorted by raw OI (highest OI). A NIFTY 26,000 CE can have high OI but be 5% OTM — it's an irrelevant wall for current session. The meaningful "CE Wall" to a trader is the **highest OI CE strike nearest to ATM** (i.e., first resistance). The UI shows the globally highest OI strike which can be a very far OTM "wall" with no practical relevance.

**Fix:** Filter `top_ce` to strikes within ±5% of LTP before sorting, or show two values: "Nearby CE Wall" and "Max CE OI Strike".

---

### 9. OI Change % in Left Panel Always Shows "+" — Negative OI Changes Hidden
**File:** `oi-spurt-scanner.html` · Line 313

```javascript
<div class="sl-oichg">+${fmt(d.oi_change_pct)}%</div>
```

**Problem:** The `+` sign is hardcoded. While NSE's spurt list by definition shows OI gainers, `avgInOI` from the NSE API can occasionally return negative values (corrections, data glitches). These would render as `+-12.34%` which is confusing.

**Fix:** Use `${d.oi_change_pct >= 0 ? '+' : ''}${fmt(d.oi_change_pct)}%`

---

### 10. Heatmap OI Change (`oi_day_high` proxy) Leaks the Same Bad Data
**File:** `oi-spurt-scanner.html` · Lines 552, 556

The heatmap shows `ΔOI` for each strike using the same corrupted `oi_chg` (derived from `oi_day_high`). This means the colour-coded ΔOI column in the heatmap is unreliable — see Issue #1.

---

## 🟡 MEDIUM — UX/Interpretation Gaps

### 11. No Market Hours Guard — Stale Data After 3:30 PM
Neither the left panel nor the right panel warns users when market is closed. The countdown timer keeps ticking and fetching NSE data after hours (NSE API returns last session's data), giving the false impression of live tracking. OI data from NSE's spurt list is **not live after 3:30 PM** — it's frozen.

**Fix:** Add IST market hours check (9:15–15:30, Mon–Fri, non-holiday). Show "Market Closed — Showing last session data" banner.

---

### 12. `Curr OI / Prev OI` in the Card Uses NSE Futures OI, Not Options OI
The `currOI` / `prevOI` displayed in section (c) comes from `spurtRow` which is the **futures OI from NSE's spurt list**. The Max Pain and PCR are computed from **options OI** (Kite chain). These are two different instruments. Showing futures OI change alongside options Max Pain without distinction misleads traders who may conflate the two.

**Fix:** Clearly label: "Futures OI Change" for the spurt row OI, and show options total OI separately.

---

### 13. Trap Labels "Short CE Trap" / "Short PE Trap" Are Non-Standard Terms
Indian retail traders know "Call Trap" and "Put Trap" from Sensibull, Opstra. "Short CE Trap" is not a recognized term in the Indian F&O community and will confuse users.

**Fix:** Rename to "CE Writer Squeeze Risk" and "PE Writer Squeeze Risk", or use recognized terminology.

---

### 14. Straddle Price is ATM CE LTP + PE LTP — Missing Bid/Ask Spread Reality
The straddle shown is mid-price. In reality, ATM straddle cost to a retail buyer is slightly higher (ask side). More importantly, showing straddle without context (e.g., "IV = X%, breakeven = ±Y points") limits its usefulness.

---

### 15. No Fallback When `NIFTYIT` or `BANKEX` is Searched
`EXCHANGE_MAP` only covers NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX. Searching `NIFTYIT` or `BANKEX` (both in the `INDICES` list on the frontend) will:
- Fall through to `NSE:NIFTYIT` which is not a valid Kite symbol → 0 LTP
- Return empty option chain (no NFO instruments named "NIFTYIT")

---

## 🟢 MINOR / GOOD PRACTICES

| Item | Status |
|------|--------|
| Instruments cache (10-min TTL) | ✅ Good — prevents repeated 5000-row downloads |
| Rate limit retry with 1s sleep | ✅ Good |
| NSE spurt list separated from Kite calls | ✅ Critical fix, correctly done |
| `strike_pcr` computed backend-side from full chain | ✅ Correct |
| 15s detail refresh, 60s left panel refresh | ✅ Appropriate cadence |
| beep alert on value change | ✅ Good UX |
| `.total_seconds()` bug in NSE session | ❌ Needs fix (Issue #4) |

---

## Priority Fix Order

| Priority | Issue | Impact |
|----------|-------|--------|
| 🔴 P0 | `oi_day_high` as prev OI → wrong buildup/trap | All signals wrong |
| 🔴 P0 | `timedelta.seconds` bug → NSE session never refreshes | Data goes stale silently |
| 🔴 P0 | `price_change` always 0 → direction always Neutral | Misleading bias signal |
| 🟠 P1 | Pivot uses today's OHLC, not previous day | Wrong S/R levels |
| 🟠 P1 | PCR thresholds wrong for stocks vs indices | Wrong sentiment label |
| 🟠 P1 | CE/PE Wall = global max OI, not nearest ATM wall | Irrelevant resistance shown |
| 🟡 P2 | No market hours guard | False "live" impression |
| 🟡 P2 | Futures OI mixed with options OI in same card | Conceptual confusion |
| 🟡 P2 | `NIFTYIT`/`BANKEX` break silently | Bad UX |
| 🟢 P3 | Hardcoded `+` on OI change % | Cosmetic |
| 🟢 P3 | Trap terminology non-standard | UX polish |
