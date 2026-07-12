import React, { useState, useEffect, useRef, useCallback } from "react";
import { LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

// ═══════════════════════════════════════════════════
// LAYER 1+2: DESIGN TOKENS
// ═══════════════════════════════════════════════════
const T = {
  bg: { base: "#080b14", subtle: "#0a0e1a", panel: "#0d1117", elevated: "#111827", overlay: "#1a2235" },
  border: { subtle: "#1a2235", default: "#1e2d45", strong: "#2d4a6b", active: "#3d7eff" },
  text: { primary: "#e8eaf0", secondary: "#8892a4", muted: "#4a5568", disabled: "#2d3548" },
  bull: { primary: "#00d084", bright: "#00ff88", dim: "#00a86b", bg: "#00d08412", bgStrong: "#00d08425" },
  bear: { primary: "#ff4444", bright: "#ff6b6b", dim: "#cc3333", bg: "#ff444412", bgStrong: "#ff444425" },
  neutral: "#f5a623",
  accent: { primary: "#3d7eff", secondary: "#6c8fff", subtle: "#3d7eff14" },
  fii: "#00bcd4", dii: "#ff9800", retail: "#e91e63",
  signal: { strong: "#00d084", moderate: "#f5a623", weak: "#ff8c00", none: "#ff4444" },
};

// ═══════════════════════════════════════════════════
// MOCK MARKET DATA
// ═══════════════════════════════════════════════════
const generateCandles = (base, count = 60) => {
  const data = [];
  let price = base;
  for (let i = count; i >= 0; i--) {
    const change = (Math.random() - 0.48) * price * 0.008;
    const open = price;
    price = Math.max(price + change, base * 0.85);
    const high = Math.max(open, price) * (1 + Math.random() * 0.003);
    const low = Math.min(open, price) * (1 - Math.random() * 0.003);
    const vol = Math.floor(Math.random() * 500000 + 100000);
    const time = new Date(Date.now() - i * 5 * 60000);
    data.push({
      time: time.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }),
      open: +open.toFixed(2), high: +high.toFixed(2),
      low: +low.toFixed(2), close: +price.toFixed(2), volume: vol,
    });
  }
  return data;
};

const INDICES = {
  NIFTY: { name: "NIFTY 50", ltp: 24385.40, change: 183.25, pct: 0.76, data: generateCandles(24200) },
  BANKNIFTY: { name: "BANK NIFTY", ltp: 52841.30, change: -124.50, pct: -0.23, data: generateCandles(52900) },
  SENSEX: { name: "SENSEX", ltp: 80124.55, change: 542.30, pct: 0.68, data: generateCandles(79600) },
  VIX: { name: "INDIA VIX", ltp: 13.42, change: -0.84, pct: -5.89, data: generateCandles(14) },
};

const WATCHLIST = [
  { sym: "RELIANCE", ltp: 2847.50, chg: 23.45, pct: 0.83, vol: "12.4L", score: 8, signal: "CALL", oi: "Long Buildup" },
  { sym: "HDFCBANK", ltp: 1742.30, chg: -8.90, pct: -0.51, vol: "8.2L", score: 7, signal: "PUT", oi: "Short Buildup" },
  { sym: "INFY", ltp: 1584.75, chg: 31.20, pct: 1.97, vol: "6.8L", score: 9, signal: "CALL", oi: "Long Buildup" },
  { sym: "TCS", ltp: 3921.60, chg: 15.30, pct: 0.39, vol: "3.1L", score: 5, signal: "NO TRADE", oi: "Neutral" },
  { sym: "ICICIBANK", ltp: 1284.45, chg: -4.20, pct: -0.33, vol: "9.4L", score: 6, signal: "CALL", oi: "Short Cover" },
  { sym: "SBIN", ltp: 824.30, chg: 12.80, pct: 1.58, vol: "15.2L", score: 8, signal: "CALL", oi: "Long Buildup" },
  { sym: "AXISBANK", ltp: 1124.55, chg: -18.40, pct: -1.61, vol: "7.6L", score: 7, signal: "PUT", oi: "Short Buildup" },
  { sym: "LT", ltp: 3684.20, chg: 44.10, pct: 1.21, vol: "2.4L", score: 6, signal: "CALL", oi: "Long Buildup" },
];

const ALERTS = [
  { id: 1, type: "bull", sym: "INFY", msg: "Breakout above ₹1,580 | Score 9/10 | Vol 2.1x", time: "10:47" },
  { id: 2, type: "bull", sym: "RELIANCE", msg: "Long buildup detected | OI +18% | FII net buy", time: "10:35" },
  { id: 3, type: "bear", sym: "HDFCBANK", msg: "Short buildup | PCR 0.68 | OI +12%", time: "10:22" },
  { id: 4, type: "system", sym: "NIFTY", msg: "PCR reading 1.34 — contrarian bullish signal", time: "10:15" },
  { id: 5, type: "bull", sym: "SBIN", msg: "Demand zone bounce | Vol 1.9x avg | Score 8/10", time: "09:58" },
];

const OI_DATA = [
  { strike: 24000, callOI: 85420, putOI: 120340 },
  { strike: 24100, callOI: 92100, putOI: 98200 },
  { strike: 24200, callOI: 145300, putOI: 87600 },
  { strike: 24300, callOI: 198400, putOI: 72400 },
  { strike: 24400, callOI: 312500, putOI: 64100 },
  { strike: 24500, callOI: 425600, putOI: 58300 },
  { strike: 24600, callOI: 284700, putOI: 44200 },
  { strike: 24700, callOI: 198200, putOI: 38900 },
];

const FII_DATA = [
  { date: "18 Apr", fii: 2840, dii: -1240 },
  { date: "17 Apr", fii: -1820, dii: 3420 },
  { date: "16 Apr", fii: 3940, dii: 1820 },
  { date: "15 Apr", fii: -2140, dii: 2840 },
  { date: "14 Apr", fii: 1640, dii: -840 },
  { date: "11 Apr", fii: 4820, dii: 2140 },
  { date: "10 Apr", fii: -980, dii: 1640 },
];

// ═══════════════════════════════════════════════════
// LAYER 3: ANIMATION CSS
// ═══════════════════════════════════════════════════
const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Syne:wght@400;500;600;700;800&display=swap');

  * { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --ease-out: cubic-bezier(0.25, 1, 0.5, 1);
    --ease-in: cubic-bezier(0.5, 0, 0.75, 0);
    --ease-spring: cubic-bezier(0.34, 1.56, 0.64, 1);
  }

  body { background: #080b14; color: #e8eaf0; font-family: 'Syne', sans-serif; overflow: hidden; }

  @keyframes pulse-ring {
    0% { transform: scale(0.8); opacity: 1; }
    100% { transform: scale(2.2); opacity: 0; }
  }
  @keyframes pulse-dot {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(0.85); }
  }
  @keyframes shimmer {
    0% { background-position: -200% 0; }
    100% { background-position: 200% 0; }
  }
  @keyframes cursor-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
  }
  @keyframes thinking-dot {
    0%, 20% { opacity: 0.2; transform: translateY(0); }
    50% { opacity: 1; transform: translateY(-4px); }
    100% { opacity: 0.2; transform: translateY(0); }
  }
  @keyframes toast-in {
    from { transform: translateX(120%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }
  @keyframes alert-flash-bull {
    0% { background: transparent; }
    25% { background: #00d08428; }
    100% { background: transparent; }
  }
  @keyframes alert-flash-bear {
    0% { background: transparent; }
    25% { background: #ff444428; }
    100% { background: transparent; }
  }
  @keyframes row-enter {
    from { opacity: 0; transform: translateX(-8px); }
    to { opacity: 1; transform: translateX(0); }
  }
  @keyframes fade-up {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes scan-line {
    0% { transform: translateY(-100%); opacity: 0.6; }
    100% { transform: translateY(100vh); opacity: 0; }
  }
  @keyframes price-tick-up {
    0%, 100% { color: #e8eaf0; }
    50% { color: #00ff88; }
  }
  @keyframes price-tick-down {
    0%, 100% { color: #e8eaf0; }
    50% { color: #ff6b6b; }
  }
  @keyframes grid-glow {
    0%, 100% { opacity: 0.03; }
    50% { opacity: 0.07; }
  }

  .skeleton {
    background: linear-gradient(90deg, #0d1117 25%, #1a2235 50%, #0d1117 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s linear infinite;
    border-radius: 4px;
  }
  .cursor { display: inline-block; width: 2px; height: 1em; background: #3d7eff; margin-left: 2px; vertical-align: text-bottom; animation: cursor-blink 800ms ease-in-out infinite; }
  .pulse-dot { position: relative; width: 8px; height: 8px; border-radius: 50%; background: #00d084; animation: pulse-dot 2s ease-in-out infinite; }
  .pulse-dot::before { content: ''; position: absolute; inset: -4px; border-radius: 50%; background: #00d084; opacity: 0.4; animation: pulse-ring 2s ease-out infinite; }
  .pulse-dot.closed { background: #ff4444; }
  .pulse-dot.closed::before { background: #ff4444; }

  .btn { transition: transform 100ms var(--ease-spring), background 100ms ease-out, border-color 100ms ease-out; cursor: pointer; border: none; outline: none; }
  .btn:active { transform: scale(0.96); }
  .btn:focus-visible { box-shadow: 0 0 0 2px #3d7eff; }

  .row-enter { animation: row-enter 200ms var(--ease-out) both; }
  .fade-up { animation: fade-up 300ms var(--ease-out) both; }

  .grid-bg {
    background-image:
      linear-gradient(rgba(61,126,255,0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(61,126,255,0.04) 1px, transparent 1px);
    background-size: 40px 40px;
    animation: grid-glow 4s ease-in-out infinite;
  }

  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #1e2d45; border-radius: 2px; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      transition-duration: 0.01ms !important;
    }
  }
`;

// ═══════════════════════════════════════════════════
// UTILITY FUNCTIONS
// ═══════════════════════════════════════════════════
const fmt = {
  price: (v) => `₹${Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
  pct: (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`,
  change: (v) => `${v >= 0 ? "+" : ""}${Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`,
  vol: (v) => v > 10000000 ? `${(v / 10000000).toFixed(1)}Cr` : v > 100000 ? `${(v / 100000).toFixed(1)}L` : `${(v / 1000).toFixed(1)}K`,
  cr: (v) => `₹${(Math.abs(v) / 100).toFixed(0)}Cr`,
};
const bull = (v) => v >= 0;
const scoreColor = (s) => s >= 8 ? T.signal.strong : s >= 6 ? T.signal.moderate : s >= 4 ? T.signal.weak : T.signal.none;
const signalColor = (s) => s === "CALL" ? T.bull.primary : s === "PUT" ? T.bear.primary : T.neutral;

// ═══════════════════════════════════════════════════
// LAYER 5: AI API CALL
// ═══════════════════════════════════════════════════
async function callClaude({ system, messages, onChunk, onComplete, onError }) {
  try {
    const response = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "claude-sonnet-4-20250514",
        max_tokens: 1000,
        stream: true,
        system,
        messages,
      }),
    });

    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.error?.message || `HTTP ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let full = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const lines = decoder.decode(value).split("\n");
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6);
        if (data === "[DONE]") continue;
        try {
          const ev = JSON.parse(data);
          if (ev.type === "content_block_delta") {
            const chunk = ev.delta?.text || "";
            full += chunk;
            onChunk?.(chunk, full);
          }
          if (ev.type === "message_stop") onComplete?.(full);
        } catch {}
      }
    }
    return full;
  } catch (e) {
    onError?.(e.message);
    throw e;
  }
}

// ═══════════════════════════════════════════════════
// COMPONENTS
// ═══════════════════════════════════════════════════

function IndexTicker({ id, idx, selected, onClick }) {
  const [flash, setFlash] = useState(null);
  const prevLtp = useRef(idx.ltp);

  useEffect(() => {
    if (idx.ltp !== prevLtp.current) {
      setFlash(idx.ltp > prevLtp.current ? "up" : "down");
      setTimeout(() => setFlash(null), 600);
      prevLtp.current = idx.ltp;
    }
  }, [idx.ltp]);

  const isBull = bull(idx.change);
  return (
    <button
      onClick={onClick}
      className="btn"
      style={{
        flex: 1, padding: "8px 16px", background: selected ? T.bg.elevated : "transparent",
        border: `1px solid ${selected ? T.border.active : T.border.subtle}`,
        borderRadius: 6, cursor: "pointer", textAlign: "left",
        transition: "all 150ms ease-out",
        boxShadow: selected ? `0 0 12px ${T.accent.primary}20` : "none",
      }}
    >
      <div style={{ fontSize: 10, color: T.text.muted, fontFamily: "'Syne'", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 2 }}>{idx.name}</div>
      <div style={{
        fontSize: 18, fontWeight: 700, fontFamily: "'JetBrains Mono'",
        color: flash === "up" ? T.bull.bright : flash === "down" ? T.bear.bright : T.text.primary,
        transition: "color 300ms ease-out",
      }}>
        {id === "VIX" ? idx.ltp.toFixed(2) : idx.ltp.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
      </div>
      <div style={{ fontSize: 12, fontFamily: "'JetBrains Mono'", color: isBull ? T.bull.primary : T.bear.primary, display: "flex", gap: 6, alignItems: "center" }}>
        <span>{isBull ? "▲" : "▼"}</span>
        <span>{fmt.change(idx.change)}</span>
        <span>({fmt.pct(idx.pct)})</span>
      </div>
    </button>
  );
}

function MiniChart({ data, color }) {
  return (
    <ResponsiveContainer width="100%" height={40}>
      <AreaChart data={data.slice(-20)} margin={{ top: 2, bottom: 2, left: 0, right: 0 }}>
        <defs>
          <linearGradient id={`grad-${color.replace("#", "")}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area type="monotone" dataKey="close" stroke={color} strokeWidth={1.5} fill={`url(#grad-${color.replace("#", "")})`} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function SignalBadge({ signal, score }) {
  const color = signalColor(signal);
  const bg = signal === "CALL" ? T.bull.bg : signal === "PUT" ? T.bear.bg : `${T.neutral}15`;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "2px 8px", borderRadius: 20, fontSize: 10, fontWeight: 700,
      fontFamily: "'JetBrains Mono'", letterSpacing: "0.05em",
      color, background: bg, border: `1px solid ${color}40`,
    }}>
      {signal === "CALL" ? "▲" : signal === "PUT" ? "▼" : "●"} {signal}
    </span>
  );
}

function ScoreBar({ score }) {
  const color = scoreColor(score);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ display: "flex", gap: 2 }}>
        {Array.from({ length: 10 }).map((_, i) => (
          <div key={i} style={{
            width: 4, height: 12, borderRadius: 2,
            background: i < score ? color : T.bg.elevated,
            transition: "background 200ms ease-out",
          }} />
        ))}
      </div>
      <span style={{ fontSize: 11, fontFamily: "'JetBrains Mono'", fontWeight: 700, color }}>{score}/10</span>
    </div>
  );
}

function MainChart({ data, indexName }) {
  const isBull = data[data.length - 1]?.close >= data[0]?.close;
  const color = isBull ? T.bull.primary : T.bear.primary;

  const CustomTooltip = ({ active, payload }) => {
    if (!active || !payload?.length) return null;
    const d = payload[0].payload;
    const chg = d.close - d.open;
    return (
      <div style={{ background: T.bg.overlay, border: `1px solid ${T.border.default}`, borderRadius: 8, padding: "10px 14px", fontFamily: "'JetBrains Mono'", fontSize: 12 }}>
        <div style={{ color: T.text.muted, fontSize: 10, marginBottom: 6 }}>{d.time}</div>
        <div style={{ color: T.text.primary }}>O: {fmt.price(d.open)}</div>
        <div style={{ color: T.bull.primary }}>H: {fmt.price(d.high)}</div>
        <div style={{ color: T.bear.primary }}>L: {fmt.price(d.low)}</div>
        <div style={{ color: bull(chg) ? T.bull.primary : T.bear.primary, fontWeight: 700 }}>C: {fmt.price(d.close)}</div>
        <div style={{ color: T.text.muted, marginTop: 4 }}>Vol: {fmt.vol(d.volume)}</div>
      </div>
    );
  };

  return (
    <div style={{ width: "100%", height: "100%" }}>
      <ResponsiveContainer width="100%" height="75%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.15} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="time" tick={{ fill: T.text.muted, fontSize: 10, fontFamily: "'JetBrains Mono'" }} tickLine={false} axisLine={false} interval={9} />
          <YAxis domain={["auto", "auto"]} tick={{ fill: T.text.muted, fontSize: 10, fontFamily: "'JetBrains Mono'" }} tickLine={false} axisLine={false} width={70} tickFormatter={(v) => v.toLocaleString("en-IN")} />
          <Tooltip content={<CustomTooltip />} />
          <Area type="monotone" dataKey="close" stroke={color} strokeWidth={2} fill="url(#chartGrad)" dot={false} />
          <ReferenceLine y={data[0]?.close} stroke={T.border.strong} strokeDasharray="4 4" strokeWidth={1} />
        </AreaChart>
      </ResponsiveContainer>
      <ResponsiveContainer width="100%" height="22%">
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <XAxis hide />
          <YAxis hide />
          <Bar dataKey="volume" fill={T.accent.primary} opacity={0.4} radius={[1, 1, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function OIChart() {
  const CustomTooltip = ({ active, payload }) => {
    if (!active || !payload?.length) return null;
    return (
      <div style={{ background: T.bg.overlay, border: `1px solid ${T.border.default}`, borderRadius: 6, padding: "8px 12px", fontSize: 11, fontFamily: "'JetBrains Mono'" }}>
        <div style={{ color: T.text.muted, marginBottom: 4 }}>Strike: {payload[0]?.payload?.strike}</div>
        <div style={{ color: T.bear.primary }}>Call OI: {payload[0]?.payload?.callOI?.toLocaleString()}</div>
        <div style={{ color: T.bull.primary }}>Put OI: {payload[0]?.payload?.putOI?.toLocaleString()}</div>
      </div>
    );
  };
  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={OI_DATA} layout="vertical" margin={{ top: 0, right: 4, bottom: 0, left: 8 }}>
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="strike" tick={{ fill: T.text.muted, fontSize: 10, fontFamily: "'JetBrains Mono'" }} tickLine={false} axisLine={false} width={40} />
        <Tooltip content={<CustomTooltip />} />
        <Bar dataKey="callOI" fill={T.bear.primary} opacity={0.7} radius={[0, 2, 2, 0]} />
        <Bar dataKey="putOI" fill={T.bull.primary} opacity={0.7} radius={[0, 2, 2, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function FIIChart() {
  return (
    <ResponsiveContainer width="100%" height={100}>
      <BarChart data={FII_DATA} margin={{ top: 0, right: 4, bottom: 0, left: 0 }}>
        <XAxis dataKey="date" tick={{ fill: T.text.muted, fontSize: 9, fontFamily: "'JetBrains Mono'" }} tickLine={false} axisLine={false} />
        <YAxis hide />
        <Tooltip
          contentStyle={{ background: T.bg.overlay, border: `1px solid ${T.border.default}`, borderRadius: 6, fontSize: 11, fontFamily: "'JetBrains Mono'" }}
          labelStyle={{ color: T.text.muted }}
          formatter={(v, name) => [fmt.cr(v), name === "fii" ? "FII" : "DII"]}
        />
        <ReferenceLine y={0} stroke={T.border.strong} />
        <Bar dataKey="fii" name="fii" fill={T.fii} opacity={0.8} radius={[2, 2, 0, 0]} />
        <Bar dataKey="dii" name="dii" fill={T.dii} opacity={0.8} radius={[2, 2, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function AlertFeed({ alerts }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {alerts.map((a, i) => {
        const color = a.type === "bull" ? T.bull.primary : a.type === "bear" ? T.bear.primary : T.accent.primary;
        const bg = a.type === "bull" ? T.bull.bg : a.type === "bear" ? T.bear.bg : T.accent.subtle;
        return (
          <div key={a.id} className="row-enter" style={{
            animationDelay: `${i * 40}ms`,
            display: "flex", alignItems: "flex-start", gap: 10,
            padding: "8px 10px", borderRadius: 6,
            background: bg, borderLeft: `2px solid ${color}`,
          }}>
            <span style={{ fontSize: 10, fontFamily: "'JetBrains Mono'", color: T.text.muted, whiteSpace: "nowrap", paddingTop: 1 }}>{a.time}</span>
            <span style={{ fontSize: 11, fontFamily: "'JetBrains Mono'", fontWeight: 700, color, minWidth: 70 }}>{a.sym}</span>
            <span style={{ fontSize: 11, color: T.text.secondary, flex: 1 }}>{a.msg}</span>
          </div>
        );
      })}
    </div>
  );
}

function ThinkingDots() {
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center", padding: "4px 0" }}>
      {[0, 0.2, 0.4].map((delay, i) => (
        <div key={i} style={{
          width: 6, height: 6, borderRadius: "50%", background: T.accent.primary,
          animation: `thinking-dot 1.2s infinite ${delay}s`,
        }} />
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════
// LAYER 5: AI ANALYSIS PANEL
// ═══════════════════════════════════════════════════
function AIAnalysisPanel({ selectedIndex, marketData }) {
  const [aiState, setAiState] = useState("idle");
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [messages, setMessages] = useState([]);
  const textRef = useRef("");
  const rafRef = useRef(null);

  const SYSTEM = `You are an expert Indian equity market analyst for NSE/BSE.
Analyze market data concisely for active traders.
Rules:
- Use ₹ for prices, IST for times
- 🟢 for bullish signals, 🔴 for bearish, ⚪ for neutral
- Always state: trend, key level, signal, risk
- Max 120 words — traders need speed
- Format with clear line breaks
- State confidence: High/Medium/Low
- End with one actionable insight
- This is educational analysis only, not SEBI registered advice`;

  const buildContext = () => ({
    index: selectedIndex,
    ltp: marketData.ltp,
    change: marketData.change,
    pct: marketData.pct,
    pcr: 1.34,
    vix: 13.42,
    fiiFlow: "+₹284Cr net buy today",
    topSignal: "INFY CALL score 9/10",
    oiWall: "24500 Call wall | 24000 Put wall",
    trend: "Price > 20EMA > 50EMA — Bullish",
  });

  const analyze = useCallback(async (userQuery) => {
    const ctx = buildContext();
    const userMsg = userQuery || `Give me a quick market analysis for ${selectedIndex} right now.`;

    const newMessages = [
      ...messages,
      { role: "user", content: `Context: ${JSON.stringify(ctx)}\n\nQuery: ${userMsg}` }
    ];

    setMessages(newMessages);
    setAiState("thinking");
    setText("");
    textRef.current = "";

    try {
      await callClaude({
        system: SYSTEM,
        messages: newMessages,
        onChunk: (chunk) => {
          textRef.current += chunk;
          if (!rafRef.current) {
            rafRef.current = requestAnimationFrame(() => {
              setText(textRef.current);
              rafRef.current = null;
            });
          }
          setAiState("streaming");
        },
        onComplete: (full) => {
          setText(full);
          setAiState("complete");
          setMessages(prev => [...prev, { role: "assistant", content: full }]);
        },
        onError: () => setAiState("error"),
      });
    } catch {
      setAiState("error");
    }
  }, [selectedIndex, messages, marketData]);

  const handleSubmit = () => {
    if (!query.trim() || aiState === "thinking" || aiState === "streaming") return;
    analyze(query);
    setQuery("");
  };

  const canSubmit = aiState === "idle" || aiState === "complete" || aiState === "error";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 8 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{
            width: 20, height: 20, borderRadius: "50%",
            background: `linear-gradient(135deg, ${T.accent.primary}, #7c4dff)`,
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 10, fontWeight: 700,
          }}>✦</div>
          <span style={{ fontSize: 12, fontWeight: 700, color: T.text.primary }}>AI ANALYSIS</span>
          <span style={{ fontSize: 10, color: T.text.muted, fontFamily: "'JetBrains Mono'" }}>claude-sonnet</span>
        </div>
        <button
          onClick={() => analyze()}
          disabled={!canSubmit}
          className="btn"
          style={{
            padding: "4px 10px", borderRadius: 4, fontSize: 10, fontWeight: 600,
            background: canSubmit ? T.accent.subtle : T.bg.elevated,
            color: canSubmit ? T.accent.primary : T.text.muted,
            border: `1px solid ${canSubmit ? T.accent.primary + "40" : T.border.subtle}`,
            cursor: canSubmit ? "pointer" : "not-allowed",
          }}
        >
          {aiState === "thinking" || aiState === "streaming" ? "..." : "↺ Refresh"}
        </button>
      </div>

      {/* Output */}
      <div style={{
        flex: 1, overflowY: "auto", padding: "10px 12px",
        background: T.bg.panel, borderRadius: 8,
        border: `1px solid ${T.border.subtle}`,
        minHeight: 120,
      }}>
        {aiState === "idle" && (
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 8 }}>
            <div style={{ fontSize: 24, opacity: 0.3 }}>✦</div>
            <p style={{ fontSize: 11, color: T.text.muted, textAlign: "center" }}>Click Refresh or ask a question<br />to get AI market analysis</p>
          </div>
        )}
        {aiState === "thinking" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ width: 16, height: 16, borderRadius: "50%", background: `linear-gradient(135deg, ${T.accent.primary}, #7c4dff)`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 8 }}>✦</div>
              <span style={{ fontSize: 11, color: T.text.muted }}>Analyzing {selectedIndex} market data...</span>
            </div>
            <ThinkingDots />
            {[100, 85, 92].map((w, i) => (
              <div key={i} className="skeleton" style={{ height: 14, width: `${w}%`, borderRadius: 4 }} />
            ))}
          </div>
        )}
        {(aiState === "streaming" || aiState === "complete") && (
          <div>
            <div style={{
              fontSize: 12, lineHeight: 1.7, color: T.text.secondary,
              fontFamily: "'Syne'", whiteSpace: "pre-wrap",
            }}>
              {text}
              {aiState === "streaming" && <span className="cursor" />}
            </div>
            {aiState === "complete" && (
              <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${T.border.subtle}`, fontSize: 10, color: T.text.muted, display: "flex", justifyContent: "space-between" }}>
                <span>✦ Generated by Claude AI</span>
                <span style={{ fontFamily: "'JetBrains Mono'" }}>Not financial advice</span>
              </div>
            )}
          </div>
        )}
        {aiState === "error" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "center", justifyContent: "center", height: "100%" }}>
            <div style={{ fontSize: 20 }}>⚠</div>
            <p style={{ fontSize: 11, color: T.bear.primary }}>Analysis failed</p>
            <button onClick={() => analyze()} className="btn" style={{ padding: "4px 12px", borderRadius: 4, fontSize: 11, background: T.bear.bg, color: T.bear.primary, border: `1px solid ${T.bear.primary}40` }}>
              Retry
            </button>
          </div>
        )}
      </div>

      {/* Input */}
      <div style={{ display: "flex", gap: 6 }}>
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === "Enter" && handleSubmit()}
          placeholder="Ask about market, OI, signals..."
          style={{
            flex: 1, padding: "8px 12px", borderRadius: 6, fontSize: 12,
            background: T.bg.panel, border: `1px solid ${T.border.default}`,
            color: T.text.primary, outline: "none", fontFamily: "'Syne'",
            transition: "border-color 150ms ease-out",
          }}
          onFocus={e => e.target.style.borderColor = T.border.active}
          onBlur={e => e.target.style.borderColor = T.border.default}
        />
        <button
          onClick={handleSubmit}
          disabled={!canSubmit || !query.trim()}
          className="btn"
          style={{
            padding: "8px 14px", borderRadius: 6, fontSize: 12, fontWeight: 600,
            background: canSubmit && query.trim() ? T.accent.primary : T.bg.elevated,
            color: canSubmit && query.trim() ? "#fff" : T.text.muted,
            border: "none", cursor: canSubmit && query.trim() ? "pointer" : "not-allowed",
          }}
        >
          ↑
        </button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════
// SIGNAL CARD
// ═══════════════════════════════════════════════════
function SignalCard({ row }) {
  const [expanded, setExpanded] = useState(false);
  const color = signalColor(row.signal);
  const bg = row.signal === "CALL" ? T.bull.bg : row.signal === "PUT" ? T.bear.bg : `${T.neutral}10`;

  return (
    <div style={{
      background: bg, borderRadius: 6, padding: "8px 10px",
      border: `1px solid ${color}25`,
      borderLeft: `2px solid ${color}`,
      transition: "all 150ms ease-out",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.text.primary }}>{row.sym}</span>
          <SignalBadge signal={row.signal} />
        </div>
        <span style={{ fontSize: 13, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: bull(row.chg) ? T.bull.primary : T.bear.primary }}>
          {fmt.price(row.ltp)}
        </span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 6 }}>
        <ScoreBar score={row.score} />
        <span style={{ fontSize: 10, color: T.text.muted, fontFamily: "'JetBrains Mono'" }}>{row.oi}</span>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════
// MAIN DASHBOARD
// ═══════════════════════════════════════════════════
export default function TradingDashboard() {
  const [selectedIdx, setSelectedIdx] = useState("NIFTY");
  const [marketOpen] = useState(true);
  const [activeTab, setActiveTab] = useState("watchlist");
  const [rightTab, setRightTab] = useState("ai");
  const [liveIndices, setLiveIndices] = useState(INDICES);
  const [time, setTime] = useState(new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));

  // Simulate live price updates
  useEffect(() => {
    const interval = setInterval(() => {
      setTime(new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      setLiveIndices(prev => {
        const updated = { ...prev };
        Object.keys(updated).forEach(k => {
          const tick = (Math.random() - 0.499) * updated[k].ltp * 0.0003;
          updated[k] = {
            ...updated[k],
            ltp: +(updated[k].ltp + tick).toFixed(2),
            change: +(updated[k].change + tick).toFixed(2),
            pct: +((updated[k].change + tick) / (updated[k].ltp) * 100).toFixed(2),
          };
        });
        return updated;
      });
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  const idx = liveIndices[selectedIdx];

  return (
    <>
      <style>{CSS}</style>
      <div style={{
        width: "100vw", height: "100vh", background: T.bg.base,
        display: "flex", flexDirection: "column", overflow: "hidden",
        fontFamily: "'Syne', sans-serif",
      }}>

        {/* TOP NAV */}
        <div style={{
          height: 48, background: T.bg.subtle, borderBottom: `1px solid ${T.border.subtle}`,
          display: "flex", alignItems: "center", padding: "0 16px", gap: 12, flexShrink: 0,
          position: "relative", zIndex: 10,
        }}>
          {/* Logo */}
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginRight: 8 }}>
            <div style={{
              width: 28, height: 28, borderRadius: 6,
              background: `linear-gradient(135deg, ${T.accent.primary}, #7c4dff)`,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 12, fontWeight: 800, color: "#fff",
            }}>T</div>
            <span style={{ fontSize: 14, fontWeight: 800, letterSpacing: "-0.02em", color: T.text.primary }}>
              TERMINUS
            </span>
          </div>

          {/* Index tickers */}
          <div style={{ display: "flex", gap: 6, flex: 1 }}>
            {Object.entries(liveIndices).map(([id, data]) => (
              <IndexTicker key={id} id={id} idx={data} selected={selectedIdx === id} onClick={() => setSelectedIdx(id)} />
            ))}
          </div>

          {/* Status */}
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginLeft: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div className={`pulse-dot ${marketOpen ? "" : "closed"}`} />
              <span style={{ fontSize: 10, fontWeight: 700, color: marketOpen ? T.bull.primary : T.bear.primary, letterSpacing: "0.06em" }}>
                {marketOpen ? "MARKET OPEN" : "MARKET CLOSED"}
              </span>
            </div>
            <div style={{ fontSize: 11, fontFamily: "'JetBrains Mono'", color: T.text.muted }}>{time} IST</div>
          </div>
        </div>

        {/* MAIN CONTENT */}
        <div style={{ flex: 1, display: "flex", overflow: "hidden", gap: 0 }}>

          {/* LEFT PANEL */}
          <div style={{
            width: 220, background: T.bg.subtle, borderRight: `1px solid ${T.border.subtle}`,
            display: "flex", flexDirection: "column", overflow: "hidden", flexShrink: 0,
          }}>
            {/* Tabs */}
            <div style={{ display: "flex", borderBottom: `1px solid ${T.border.subtle}`, flexShrink: 0 }}>
              {["watchlist", "signals"].map(tab => (
                <button key={tab} onClick={() => setActiveTab(tab)} className="btn" style={{
                  flex: 1, padding: "8px 4px", fontSize: 10, fontWeight: 700,
                  textTransform: "uppercase", letterSpacing: "0.06em",
                  background: "transparent", color: activeTab === tab ? T.accent.primary : T.text.muted,
                  borderBottom: `2px solid ${activeTab === tab ? T.accent.primary : "transparent"}`,
                  borderRadius: 0, transition: "all 150ms ease-out",
                }}>{tab}</button>
              ))}
            </div>

            <div style={{ flex: 1, overflowY: "auto", padding: 8 }}>
              {activeTab === "watchlist" ? (
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  {/* Header */}
                  <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 6px", marginBottom: 4 }}>
                    <span style={{ fontSize: 9, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em" }}>SYMBOL</span>
                    <span style={{ fontSize: 9, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em" }}>LTP / CHG</span>
                  </div>
                  {WATCHLIST.map((row, i) => (
                    <div key={row.sym} className="row-enter" style={{
                      animationDelay: `${i * 30}ms`,
                      display: "flex", justifyContent: "space-between", alignItems: "center",
                      padding: "6px 8px", borderRadius: 5, cursor: "pointer",
                      borderLeft: `2px solid ${signalColor(row.signal)}40`,
                      transition: "background 100ms ease-out",
                    }}
                      onMouseEnter={e => e.currentTarget.style.background = T.bg.elevated}
                      onMouseLeave={e => e.currentTarget.style.background = "transparent"}
                    >
                      <div>
                        <div style={{ fontSize: 12, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.text.primary }}>{row.sym}</div>
                        <div style={{ fontSize: 9, color: T.text.muted, marginTop: 1 }}>{row.vol} vol</div>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        <div style={{ fontSize: 12, fontFamily: "'JetBrains Mono'", color: T.text.primary }}>{row.ltp.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</div>
                        <div style={{ fontSize: 10, fontFamily: "'JetBrains Mono'", color: bull(row.chg) ? T.bull.primary : T.bear.primary }}>{fmt.pct(row.pct)}</div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {WATCHLIST.filter(r => r.signal !== "NO TRADE").map((row, i) => (
                    <div key={row.sym} className="row-enter" style={{ animationDelay: `${i * 40}ms` }}>
                      <SignalCard row={row} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* CENTER — MAIN CHART */}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>
            {/* Grid background */}
            <div className="grid-bg" style={{ position: "absolute", inset: 0, pointerEvents: "none", zIndex: 0 }} />

            {/* Chart header */}
            <div style={{ padding: "10px 16px 6px", display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: `1px solid ${T.border.subtle}`, position: "relative", zIndex: 1, background: T.bg.base + "dd", backdropFilter: "blur(4px)" }}>
              <div>
                <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
                  <span style={{ fontSize: 16, fontWeight: 800, color: T.text.primary, letterSpacing: "-0.02em" }}>{idx.name}</span>
                  <span style={{ fontSize: 22, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.text.primary }}>
                    {selectedIdx === "VIX" ? idx.ltp.toFixed(2) : idx.ltp.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
                  </span>
                  <span style={{ fontSize: 14, fontFamily: "'JetBrains Mono'", color: bull(idx.change) ? T.bull.primary : T.bear.primary }}>
                    {bull(idx.change) ? "▲" : "▼"} {fmt.change(idx.change)} ({fmt.pct(idx.pct)})
                  </span>
                </div>
                <div style={{ fontSize: 10, color: T.text.muted, marginTop: 2 }}>
                  <span style={{ fontFamily: "'JetBrains Mono'" }}>5M · NSE · </span>
                  <span style={{ color: T.text.disabled }}>15-min delayed (yfinance) · Live: Kite API</span>
                </div>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                {["5M", "15M", "1H", "1D"].map(tf => (
                  <button key={tf} className="btn" style={{
                    padding: "4px 10px", borderRadius: 4, fontSize: 10, fontWeight: 700,
                    background: tf === "5M" ? T.accent.subtle : "transparent",
                    color: tf === "5M" ? T.accent.primary : T.text.muted,
                    border: `1px solid ${tf === "5M" ? T.accent.primary + "40" : T.border.subtle}`,
                  }}>{tf}</button>
                ))}
              </div>
            </div>

            {/* Chart */}
            <div style={{ flex: 1, padding: "8px 8px 4px", position: "relative", zIndex: 1 }}>
              <MainChart data={idx.data} indexName={selectedIdx} />
            </div>

            {/* Alert feed */}
            <div style={{
              height: 180, borderTop: `1px solid ${T.border.subtle}`,
              background: T.bg.subtle + "cc", backdropFilter: "blur(4px)",
              display: "flex", flexDirection: "column", position: "relative", zIndex: 1,
            }}>
              <div style={{ padding: "6px 12px", borderBottom: `1px solid ${T.border.subtle}`, display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: T.text.muted, textTransform: "uppercase", letterSpacing: "0.08em" }}>ALERT FEED</span>
                <span style={{ fontSize: 10, fontFamily: "'JetBrains Mono'", background: T.accent.subtle, color: T.accent.primary, padding: "1px 6px", borderRadius: 10, fontWeight: 700 }}>{ALERTS.length}</span>
              </div>
              <div style={{ flex: 1, overflowY: "auto", padding: "6px 10px" }}>
                <AlertFeed alerts={ALERTS} />
              </div>
            </div>
          </div>

          {/* RIGHT PANEL */}
          <div style={{
            width: 280, background: T.bg.subtle, borderLeft: `1px solid ${T.border.subtle}`,
            display: "flex", flexDirection: "column", overflow: "hidden", flexShrink: 0,
          }}>
            {/* Tabs */}
            <div style={{ display: "flex", borderBottom: `1px solid ${T.border.subtle}`, flexShrink: 0 }}>
              {[
                { id: "ai", label: "✦ AI" },
                { id: "oi", label: "OI" },
                { id: "fii", label: "FII/DII" },
              ].map(tab => (
                <button key={tab.id} onClick={() => setRightTab(tab.id)} className="btn" style={{
                  flex: 1, padding: "8px 4px", fontSize: 10, fontWeight: 700,
                  textTransform: "uppercase", letterSpacing: "0.06em",
                  background: "transparent",
                  color: rightTab === tab.id ? T.accent.primary : T.text.muted,
                  borderBottom: `2px solid ${rightTab === tab.id ? T.accent.primary : "transparent"}`,
                  borderRadius: 0, transition: "all 150ms ease-out",
                }}>{tab.label}</button>
              ))}
            </div>

            <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
              {rightTab === "ai" && (
                <div style={{ flex: 1, padding: 10, overflow: "hidden", display: "flex", flexDirection: "column" }}>
                  <AIAnalysisPanel selectedIndex={selectedIdx} marketData={idx} />
                </div>
              )}

              {rightTab === "oi" && (
                <div style={{ flex: 1, overflowY: "auto", padding: 10 }}>
                  {/* PCR */}
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>PUT-CALL RATIO</div>
                    <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", background: T.bg.panel, borderRadius: 8, border: `1px solid ${T.border.subtle}` }}>
                      <div style={{ fontSize: 28, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.bull.primary }}>1.34</div>
                      <div>
                        <div style={{ fontSize: 11, color: T.bull.primary, fontWeight: 600 }}>Contrarian Bullish</div>
                        <div style={{ fontSize: 10, color: T.text.muted }}>Crowd over-hedged</div>
                      </div>
                    </div>
                    {/* PCR bar */}
                    <div style={{ marginTop: 8, height: 6, borderRadius: 3, background: T.bg.elevated, overflow: "hidden" }}>
                      <div style={{ width: "67%", height: "100%", background: `linear-gradient(90deg, ${T.bear.primary}, ${T.bull.primary})`, borderRadius: 3 }} />
                    </div>
                    <div style={{ display: "flex", justifyContent: "space-between", marginTop: 2 }}>
                      <span style={{ fontSize: 9, color: T.bear.primary, fontFamily: "'JetBrains Mono'" }}>0.7 Bear</span>
                      <span style={{ fontSize: 9, color: T.bull.primary, fontFamily: "'JetBrains Mono'" }}>1.3 Bull</span>
                    </div>
                  </div>

                  {/* OI chart */}
                  <div>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
                      OI BY STRIKE — {selectedIdx}
                      <span style={{ marginLeft: 8, color: T.bear.primary }}>■ Call</span>
                      <span style={{ marginLeft: 6, color: T.bull.primary }}>■ Put</span>
                    </div>
                    <OIChart />
                  </div>

                  {/* Max pain */}
                  <div style={{ marginTop: 10, padding: "8px 12px", background: T.bg.panel, borderRadius: 6, border: `1px solid #ffd70030`, display: "flex", justifyContent: "space-between" }}>
                    <span style={{ fontSize: 11, color: T.text.secondary }}>Max Pain</span>
                    <span style={{ fontSize: 13, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: "#ffd700" }}>24,300</span>
                  </div>
                  <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
                    <div style={{ flex: 1, padding: "8px 10px", background: T.bear.bg, borderRadius: 6, border: `1px solid ${T.bear.primary}30` }}>
                      <div style={{ fontSize: 9, color: T.text.muted, marginBottom: 2 }}>CALL WALL</div>
                      <div style={{ fontSize: 13, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.bear.primary }}>24,500</div>
                    </div>
                    <div style={{ flex: 1, padding: "8px 10px", background: T.bull.bg, borderRadius: 6, border: `1px solid ${T.bull.primary}30` }}>
                      <div style={{ fontSize: 9, color: T.text.muted, marginBottom: 2 }}>PUT WALL</div>
                      <div style={{ fontSize: 13, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: T.bull.primary }}>24,000</div>
                    </div>
                  </div>
                </div>
              )}

              {rightTab === "fii" && (
                <div style={{ flex: 1, overflowY: "auto", padding: 10 }}>
                  {/* Today summary */}
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>TODAY — 23 APR IST</div>
                    <div style={{ display: "flex", gap: 6 }}>
                      {[
                        { label: "FII", value: "+₹284Cr", color: T.fii },
                        { label: "DII", value: "-₹142Cr", color: T.dii },
                        { label: "RETAIL", value: "-₹98Cr", color: T.retail },
                      ].map(p => (
                        <div key={p.label} style={{ flex: 1, padding: "8px 8px", background: T.bg.panel, borderRadius: 6, border: `1px solid ${p.color}20` }}>
                          <div style={{ fontSize: 9, color: T.text.muted, marginBottom: 3, fontWeight: 700 }}>{p.label}</div>
                          <div style={{ fontSize: 12, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: p.color }}>{p.value}</div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* FII chart */}
                  <div>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>
                      7-DAY FLOW
                      <span style={{ marginLeft: 8, color: T.fii }}>■ FII</span>
                      <span style={{ marginLeft: 6, color: T.dii }}>■ DII</span>
                    </div>
                    <FIIChart />
                  </div>

                  {/* Institutional bias */}
                  <div style={{ marginTop: 12 }}>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>INSTITUTIONAL BIAS</div>
                    {[
                      { label: "FII Index Futures", val: "Net Long +12,400 contracts", bull: true },
                      { label: "FII Options", val: "Net Short -8,200 PE written", bull: true },
                      { label: "DII Equity", val: "Net Seller -₹142Cr", bull: false },
                    ].map(r => (
                      <div key={r.label} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: `1px solid ${T.border.subtle}` }}>
                        <span style={{ fontSize: 11, color: T.text.muted }}>{r.label}</span>
                        <span style={{ fontSize: 10, fontFamily: "'JetBrains Mono'", color: r.bull ? T.bull.primary : T.bear.primary }}>{r.val}</span>
                      </div>
                    ))}
                  </div>

                  {/* 4-layer score */}
                  <div style={{ marginTop: 12, padding: "10px 12px", background: T.bg.panel, borderRadius: 8, border: `1px solid ${T.border.subtle}` }}>
                    <div style={{ fontSize: 10, color: T.text.muted, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 8 }}>4-LAYER SIGNAL — NIFTY</div>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <span style={{ fontSize: 14, fontWeight: 800, color: T.bull.primary }}>CALL</span>
                      <ScoreBar score={8} />
                    </div>
                    {[
                      { layer: "L1 Structure", score: "+2/2", status: "✅", color: T.bull.primary },
                      { layer: "L2 F&O", score: "+4/4", status: "✅", color: T.bull.primary },
                      { layer: "L3 Volume", score: "+2/2", status: "✅", color: T.bull.primary },
                      { layer: "L4 IV", score: "+0/1", status: "⚠", color: T.neutral },
                    ].map(l => (
                      <div key={l.layer} style={{ display: "flex", justifyContent: "space-between", padding: "4px 0", borderBottom: `1px solid ${T.border.subtle}` }}>
                        <span style={{ fontSize: 10, color: T.text.secondary }}>{l.status} {l.layer}</span>
                        <span style={{ fontSize: 10, fontFamily: "'JetBrains Mono'", fontWeight: 700, color: l.color }}>{l.score}</span>
                      </div>
                    ))}
                    <div style={{ marginTop: 6, fontSize: 10, color: T.text.muted }}>Conflicts: None ✅ · Trend: BULL ↑</div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
