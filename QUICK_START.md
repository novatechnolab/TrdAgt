# FNO Session Analyzer - Quick Start Guide

## 🚀 Get Started in 2 Minutes

### 1. Start the App
```bash
cd /home/rajk/Downloads/TradeSignal
source .venv/bin/activate
python app/backend/server.py
```

### 2. Open Browser
```
http://localhost:5000
```

### 3. Connect Kite API (Settings page)
- Enter your Kite API Key
- Complete Kite OAuth login
- Confirm "Connected ✓" status

### 4. Click "⏰ FNO Sessions" in Sidebar

### 5. Select a Stock & Click "Quick Analysis"

Done! You'll see:
- 📊 Overall Score (0–100)
- 📈 Direction (BULLISH/BEARISH/NEUTRAL)
- 🎯 Trading Strategy
- 💡 Key Signals & Alerts

---

## 🕐 What Each Session Means

### 🌅 Premarket (6:00–8:59 AM)
**Today's opening setup is being formed**
- Gap up/down from yesterday?
- Big news overnight?
- FII buying or selling?
- OI changing?
→ **Trade:** Place orders at market open

### 🔔 Opening Bell (9:00–9:15 AM)
**First 15 minutes tell the story**
- Strong opening momentum?
- Volume pouring in?
- PCR (put/call ratio) shifting?
- IV expanding or crushing?
→ **Trade:** Buy or sell options spreads

### ⚡ Live (9:15 AM–3:30 PM)
**Real-time money-making opportunity**
- Price trending up or down?
- Where is max pain?
- PCR swinging?
- IV compressing?
→ **Trade:** Execute your best setups

---

## 📊 What the Score Means

| Score | Meaning | Action |
|-------|---------|--------|
| **≥ 80** | Very Strong Signal | EXECUTE trade immediately |
| **70–79** | Strong Signal | Enter with full conviction |
| **60–69** | Moderate Signal | Use smaller position size |
| **50–59** | Weak Signal | Monitor, don't trade yet |
| **< 50** | Wait Signal | Sit tight, next opportunity coming |

---

## 🎯 Example Analysis Result

```
Session: ⚡ LIVE TRADING (2:30 PM)

RELIANCE      Score: 82      BULLISH      HIGH Confidence
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💯 Factor Breakdown:
   Price Action        25/25  [████████████] ← Strong uptrend
   PCR & Max Pain      22/25  [███████████ ] ← Calls winning
   Volume              18/20  [███████████ ] ← Heavy volume
   IV Dynamics         14/15  [████████████] ← Expanding
   Momentum Confirm    16/15  [████████████] ← RSI 65+

📋 Recommendation: BUY_CALL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Strategy:
  Setup:    Strong bullish momentum with call OI dominance
  Trade:    Buy ATM Call (2850 strike)
  Entry:    At 2850.50 or on any dip to 2845
  Target 1: 2865 (+0.5%)
  Target 2: 2880 (+1.1%)
  Stop:     2835 (-0.5%)
  R:R:      1:1.5

⚡ Key Signals:
  ✓ Price above all moving averages
  ✓ Call OI increasing
  ✓ Intraday volume 1.8x average
  ✓ RSI at 68 (strong but not overbought)
```

---

## 💡 Pro Tips

### 🔄 PCR Swing Signals
> When Put-Call ratio changes > 10% in 15 min = Major reversal risk!
- High PCR (puts winning) on UP day = Likely pullback
- Low PCR (calls winning) on DOWN day = Likely bounce

### 🎯 Max Pain Magic
> Price tends to close AWAY from max pain at expiry
- Max pain at 2860 but price at 2840 = Tend UPWARD
- Use this for directional bias

### 📊 Volume > Price
> If volume is 2x normal, price move is likely real
- 1.5x+ volume with directional move = Strong signal
- Low volume move = Likely to reverse

### ⏰ Time Matters
- **9:00–9:15 AM:** Most volatile, best setups
- **9:15–11:00 AM:** Momentum confirmed, chase trends
- **11:00 AM–1:00 PM:** Middle hour, choppy, avoid
- **1:00–3:30 PM:** Final push, big moves often start here

---

## ⚙️ One-Time Setup

### API Key from Kite
1. Go to: https://kite.trade
2. Login to your account
3. Go to Settings → API Keys
4. Create new app or find existing
5. Get **API Key** and **API Secret**

### First Login
1. Click "Kite Login" in Settings
2. This opens Kite OAuth
3. You'll get redirected back with a request_token
4. App auto-generates access_token
5. You're connected! ✓

---

## 🐛 Troubleshooting

### "Market is closed"
- App shows this outside 6:00 AM - 3:30 PM IST
- Normal! Comes back next trading day

### "Kite API not connected"
- Go to Settings
- Re-enter API Key
- Click "Test Connection"
- Check that access_token is populated

### "No data available"
- Make sure Kite API is connected
- Run full scoring first (Dashboard → Run Scoring Engine)
- Then try FNO Sessions again

---

## 📈 Backtesting Ideas

Want to validate these strategies?

1. **Run analysis on Monday** - Note all HIGH-confidence signals
2. **Check results on Friday** - Were they right?
3. **Track win rate** - 60%+ is excellent for real trading
4. **Adjust thresholds** - If too many false signals, raise score threshold

---

## 🎓 Learn More

- **Inside HTML:** Check `page-fno-session` for UI structure
- **Analysis Code:** Read `fno-session-analyzer.js` for scoring rules
- **Full Docs:** See `FNO_SESSION_ANALYZER.md` for complete details

---

## 🚀 You're Ready!

The FNO Session Analyzer is built for **intraday F&O traders** who want:
- ✅ Quick buy/sell signals
- ✅ Session-aware strategies
- ✅ Real-time PCR & max pain tracking
- ✅ Confidence scores for position sizing

**Happy Trading! 📈**

---

*Built with ❤️ for NSE F&O traders*
*Last Updated: April 6, 2026*
