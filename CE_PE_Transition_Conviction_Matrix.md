# CE & PE OI Transition Matrix — Conviction Scoring
### Companion to CE_PE_OI_State_Combination_KB.md — this covers single-leg FROM→TO transitions, not just destination state.

Rule of thumb used throughout: a transition into a state via a **sharp flip** (skipping the "unwind" phase) carries **higher conviction** than one reached gradually. A transition **out of an active SB/SC into a passive LU** signals **fading conviction**, not reversal. A transition reached via the "natural" adjacent state (e.g., SB→LU→LB) is more trustworthy than one that jumps (e.g., LB→SB directly).

---

## 1. CE (Call) Leg — Transition Matrix

Rows = FROM state, Columns = TO state. Cell = Bias (Conviction Strength).

| FROM \ TO | → LB | → LU | → SB | → SC |
|---|---|---|---|---|
| **LB** (from) | Bullish (High) — continuation | Neutral/Mild Bearish (Low) — profit booking | **Bearish (High)** — sharp reversal, bulls trapped | Bullish (Medium-High) — rally continues, mechanism shifts to short-covering |
| **LU** (from) | Bullish (Medium) — re-accumulation, needs confirmation | Bearish (Medium) — passive continuation | Bearish (High) — passive fade hardens into fresh writing | Bullish (Medium-High) — bottoming, shorts starting to cover |
| **SB** (from) | **Bullish (High)** — writers overwhelmed, short squeeze | Bearish (Medium-Low) — **conviction fading**, not yet reversal | Bearish (High) — sustained resistance | **Bullish (High)** — writers capitulate, resistance breaks |
| **SC** (from) | Bullish (High) — fresh buyers extend the squeeze | Bearish (Medium) — squeeze momentum fading | Bearish (Medium-High) — fresh resistance re-forms | Bullish (High) — sustained squeeze continuation |

---

## 2. PE (Put) Leg — Transition Matrix

Rows = FROM state, Columns = TO state. Cell = Bias (Conviction Strength).

| FROM \ TO | → LB | → LU | → SB | → SC |
|---|---|---|---|---|
| **LB** (from) | Bearish (High) — continuation | Neutral/Mild Bullish (Low) — profit booking | **Bullish (High)** — sharp reversal, put buyers trapped | Bearish (Medium-High) — decline continues, mechanism shifts to short-covering |
| **LU** (from) | Bearish (Medium) — re-accumulation, needs confirmation | Bullish (Medium) — passive continuation | Bullish (High) — passive fade hardens into fresh writing | Bearish (Medium-High) — topping, shorts starting to cover |
| **SB** (from) | **Bearish (High)** — writers overwhelmed, support breaks hard | Bullish (Medium-Low) — **conviction fading**, not yet reversal | Bullish (High) — sustained support | **Bearish (High)** — writers capitulate, support breaks |
| **SC** (from) | Bearish (High) — fresh sellers extend the breakdown | Bullish (Medium) — breakdown momentum fading | Bullish (Medium-High) — fresh support re-forms | Bearish (High) — sustained breakdown continuation |

---

## 3. Reading the Diagonal vs. Off-Diagonal

- **Diagonal cells (LB→LB, LU→LU, SB→SB, SC→SC)** = sustained state, no transition. These are your highest-conviction *continuation* signals because the same participant behavior is persisting.
- **Adjacent transitions** (LB→LU, LU→SB, SB→SC, SC→LB and their reverses) = the "natural" lifecycle of a position — buildup → unwind → opposite buildup. These are lower-drama, more trustworthy conviction reads.
- **Diagonal-skip transitions** (LB→SB, SB→LB, SC→LU, LU→SC) = sharp flips. These carry the *highest single-transition conviction* because they represent one side being actively overrun by the other in a single snapshot — but also carry the highest false-signal/trap risk on illiquid strikes, since a sharp OI flip can be a single large order rather than broad participation.

---

## 4. Applying This to Your Earlier Example

**CE: LB→SB** and **PE: SB→LU**, occurring together:

| Leg | Transition | Table Lookup | Conviction |
|---|---|---|---|
| CE | LB → SB | Bearish (High) — sharp reversal, bulls trapped | High |
| PE | SB → LU | Bullish (Medium-Low) — conviction fading, not reversal | Medium-Low (and bullish-leaning, i.e. bearish for price working against it) |

**Combined:** CE leg contributes a strong, fresh bearish signal. PE leg contributes only a weak fading-bullish signal (support conviction eroding, not active bearish participation). Net read: **bearish, but driven almost entirely by the call side** — the put side is a lagging, low-conviction confirmation at best. This matches what we flagged earlier: treat it as higher trap-risk than the static combination table suggests, and weight the CE leg's fresh reversal more heavily than the PE leg's passive fade in your composite score.

---

## 5. Suggested Scoring Formula for Your Rule Engine

```
leg_score(leg) = direction(leg) × strength_weight(leg)

where:
  direction  = +1 (bullish) or -1 (bearish)
  strength_weight = High: 1.0 | Medium-High: 0.75 | Medium: 0.5 | Medium-Low: 0.35 | Low: 0.2

composite_conviction = leg_score(CE) + leg_score(PE)

  |composite| >= 1.5  -> High conviction directional signal
  |composite| 0.7–1.5 -> Medium conviction, confirm with spot/volume
  |composite| < 0.7    -> Low conviction / noise, do not trade in isolation

---

## 6. Flat / Inactive (Neutral) Transitions

To prevent "black holes" when moving out of or into low-activity periods (where premium/OI changes fall below filters):

### CE (Call) Leg:
* `Flat → LB`: Bullish (Medium) — call buyers start accumulation
* `Flat → SB`: Bearish (Medium) — call writers start selling
* `Flat → LU`: Bearish (Low) — minor call unwinding
* `Flat → SC`: Bullish (Low) — minor short covering
* `LB / SC → Flat`: Bearish (Low) — bullish buying/covering momentum drying up
* `SB / LU → Flat`: Bullish (Low) — bearish writing/unwinding momentum drying up
* `Flat → Flat`: Neutral (Low) — consolidation continues

### PE (Put) Leg:
* `Flat → LB`: Bearish (Medium) — put buyers start accumulation
* `Flat → SB`: Bullish (Medium) — put writers start selling
* `Flat → LU`: Bullish (Low) — minor put unwinding
* `Flat → SC`: Bearish (Low) — minor short covering
* `LB / SC → Flat`: Bullish (Low) — bearish buying/covering momentum drying up
* `SB / LU → Flat`: Bearish (Low) — bullish writing/unwinding momentum drying up
* `Flat → Flat`: Neutral (Low) — consolidation continues
```

This lets a sharp CE flip (weight 1.0) dominate a fading PE signal (weight 0.35) in the composite, rather than both legs being weighted equally as your static destination-state matrix implicitly does.
