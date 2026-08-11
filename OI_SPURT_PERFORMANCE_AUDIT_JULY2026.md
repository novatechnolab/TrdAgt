# OI Spurt Scanner Performance & Data Refresh Audit Outcome

**Date:** July 27, 2026  
**Auditor / Engine:** Claude Sonnet 4.6 (Thinking)  
**Topic:** Performance Audit & Off-Hours Staleness Control Architecture for OI Spurt Scanner

---

## 1. Core Problem Addressed
During out-of-market hours, data is static. Continuing to run 15-second option chain quote loops and 60-second NSE spurt list scrapers outside market hours wastes server resources and API quota while creating a misleading UI where timers tick and spinners spin on unchanging data.

Conversely, aggressive caching creates a staleness risk if the UI displays cached data without telling the user that it is an EOD snapshot.

---

## 2. The 4-Layer Performance & Staleness Architecture

### Layer 1: Backend Cache with Metadata Tags
Every API response from `/api/oi/spurt` and `/api/oi/symbol/<sym>` is enriched with explicit staleness metadata:

```json
{
  "data": [...],
  "market_open": false,
  "cache_source": "eod_cache",           // "live" | "eod_cache" | "hist_cache"
  "data_as_of": "2026-07-27T15:30:00"   // Exact timestamp when data was fetched
}
```

#### Caching Strategy Matrix:
| Data Type | Cache Key Format | Invalidation Rule | Staleness Risk |
|---|---|---|---|
| **NSE Spurt List** | `(date_str, min_pct)` | Flushes on next trading day `date_str` | Zero (EOD data is static) |
| **Historical Pivots** | `(symbol, date_str)` | Flushes on next trading day `date_str` | Zero (Pivots use previous day EOD) |
| **Option Chain Quotes** | `(symbol, timestamp // 5)` | 5s TTL intraday / 5min TTL off-hours | Negligible intraday |
| **Market Hours Flag** | Computed live IST | 9:10 AM to 3:35 PM IST check | None |

---

### Layer 2: Frontend Visual Indicators & Timer Controls

#### Spurt List (Left Panel - 60s Timer):
* When `market_open == false`: Replaces the active countdown timer with a **`"Market Closed — EOD Snapshot"`** amber badge.
* Displays `data_as_of` timestamp: *"Data as of 15:30 IST"*.
* Halts countdown animation so no fake "refreshing" spinners occur.
* On manual refresh click: serves cached EOD data once without running a full API loading cycle.

#### Symbol Detail (Right Panel - 15s Timer):
* When `market_open == false`: Replaces *"Auto-refresh 15s"* badge with **`"🌙 Market Closed"`**.
* Triggers `clearInterval(tabTimers[sym])` to stop 15-second background requests entirely.
* Displays `data_as_of` in the stat strip: *"EOD 27 Jul — 15:30"*.

---

### Layer 3: Market Open Lifecycle Transition (9:15 AM IST)

| Time (IST) | Market Event | Backend Behavior | Frontend Behavior |
|---|---|---|---|
| **09:14 AM** | Pre-open / Closed | Returns `eod_cache`, `market_open: false` | Displays *"Market Closed"* amber badge |
| **09:15 AM** | **Market Opens** | `is_market_open() = true` (cache bypassed) | Badges disappear; timers auto-restart |
| **09:15:05** | First Live Fetch | Fresh NSE scrape + fresh Kite option chain quotes | `data_as_of` updates to `09:15:05 IST` |
| **09:15–15:30** | Live Market Session | 60s spurt list + 15s detail refresh loops | Active live indicators and alert badges |
| **15:31 PM** | Market Closes | Stores last fetch as `eod_cache` | Serves `eod_cache`; displays *"Market Closed"* |

---

### Layer 4: Market Open Lifecycle & Invalidation
* Date-switch auto-invalidation on new trading days (`date_str` change) and auto-restarting the 60s/15s refresh loops when `is_market_open()` flips to `True` at 9:15 AM IST.

---

## 3. Two Bulletproof Guarantees

1. **Exact Timestamp Visibility:** The UI always displays `data_as_of`, allowing users to detect whether they are looking at live market data or an EOD snapshot.
2. **Timer Halt on Market Close:** Setting `market_open: false` automatically clears interval timers (`clearInterval`), eliminating background network calls when markets are closed.
