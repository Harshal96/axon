/* Charts — EvilCharts-inspired: hand-rolled SVG, gradient fill, glow dot,
   CSS-var themed, animated draw. No chart library shipped;
   swap path: @evilcharts/recharts-* behind this same {data,color} API. */
import { useId } from "react";
export function Spark({ data, color = "var(--ok)", w = 72, h = 22 }: { data: number[]; color?: string; w?: number; h?: number }) {
  const id = useId();
  if (data.length < 2) return <span className="dim">—</span>;
  const max = Math.max(...data) * 1.15 || 1;
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - (v / max) * (h - 3) - 1] as const);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${w},${h} L0,${h} Z`;
  const last = pts[pts.length - 1];
  return (
    <svg width={w} height={h} style={{ overflow: "visible", flex: "none" }} aria-hidden>
      <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor={color} stopOpacity=".28" /><stop offset="100%" stopColor={color} stopOpacity="0" />
      </linearGradient></defs>
      <path d={area} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" pathLength={1}
        style={{ strokeDasharray: 1, animation: "draw 1s var(--ease) both" }} />
      <circle cx={last[0]} cy={last[1]} r="2.4" fill={color} style={{ filter: `drop-shadow(0 0 4px ${color})` }} />
    </svg>
  );
}
export function Ring({ pct, size = 15 }: { pct: number; size?: number }) {
  const r = (size - 3) / 2, c = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} style={{ transform: "rotate(-90deg)", flex: "none" }} aria-label={`${pct}%`}>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--line)" strokeWidth="2.4" />
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={pct >= 100 ? "var(--ok)" : "var(--tx-2)"} strokeWidth="2.4"
        strokeLinecap="round" strokeDasharray={`${(pct / 100) * c} ${c}`} style={{ transition: "stroke-dasharray .6s var(--ease)" }} />
    </svg>
  );
}
export function AreaChart({ data, labels }: { data: number[]; labels: string[] }) {
  const id = useId(), w = 660, h = 120, max = Math.max(...data) * 1.2 || 1;
  const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - (v / max) * (h - 14) - 4] as const);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", display: "block" }} role="img" aria-label="Arc progress over time">
        <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity=".22" /><stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient></defs>
        {[.25, .5, .75].map(f => <line key={f} x1="0" x2={w} y1={h * f} y2={h * f} stroke="var(--line-soft)" strokeDasharray="3 5" />)}
        <path d={`${line} L${w},${h} L0,${h} Z`} fill={`url(#${id})`} />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" />
        {pts.map((p, i) => <circle key={i} cx={p[0]} cy={p[1]} r="2.6" fill="var(--bg)" stroke="var(--accent)" strokeWidth="1.6" />)}
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", color: "var(--tx-3)", fontSize: 11, marginTop: 4 }}>
        {labels.map(l => <span key={l}>{l}</span>)}
      </div>
    </div>
  );
}
