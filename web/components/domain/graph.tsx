/* Domain graph — the per-API ego graph.
 *
 * Call direction is shown three ways, so it reads even with motion off:
 * an arrowhead, a marching dash, and packets travelling the path.
 * Packets use SVG animateMotion; when the user prefers reduced motion
 * (or pauses), only the arrowheads remain. Edge kinds that are not
 * calls (EXPOSES) never get packets.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Maximize, Minus, Pause, Play, Plus } from "lucide-react";
import { usePrefersReducedMotion } from "@base/hooks";
import type { ApiGraph, GNode, Route } from "@base/types";
import {
  BOX_H, BOX_W, egoEdges, elbowPath, elbowPathVertical, flowPair,
  focusSet, layoutGraph, visibleGraphNodes, truncateEnd, truncateMiddle,
} from "@base/layout";
import { NodeInspector } from "./inspector";
import { nodeLabel } from "@base/catalog";

type FlowTone = "call" | "code" | "meta";

function FlowEdge({ id, d, tone, animate, packets = 1, label }: {
  id: string; d: string; tone: FlowTone; animate: boolean;
  packets?: number; label?: string;
}) {
  const moving = animate && tone !== "meta";
  const n = Math.min(3, Math.max(1, packets));
  return (
    <g className={`flowedge tone-${tone}`}>
      {label && <title>{label}</title>}
      <path id={id} d={d} className="gedge-base" fill="none" markerEnd="url(#flow-arrow)" />
      {moving && <path d={d} className="gedge-dash" fill="none" aria-hidden />}
      {moving && Array.from({ length: n }, (_, i) => (
        <circle key={i} r={tone === "call" ? 3 : 2.2} className="gpacket" aria-hidden>
          <animateMotion dur="2.8s" begin={`${(-i * 2.8) / n}s`} repeatCount="indefinite">
            <mpath href={`#${id}`} />
          </animateMotion>
        </circle>
      ))}
    </g>
  );
}

/* Shared defs: the flow arrowhead, plus one node clip. The clip is in
   the referencing node's local space (each node <g> is translated to
   its box), so a single 0,0 rect clips every node's text to its box —
   labels are fitted first, this is the hard guard against painting
   outside the box with an unexpected font fallback. */
function GraphDefs({ clipId, w, h, rx }: {
  clipId: string; w: number; h: number; rx: number;
}) {
  return (
    <defs>
      <marker id="flow-arrow" viewBox="0 0 10 10" refX="9" refY="5"
        markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 1 L 10 5 L 0 9 z" style={{ fill: "var(--tx-3)" }} />
      </marker>
      <clipPath id={clipId}>
        <rect x={1} y={1} width={w - 2} height={h - 2} rx={rx} />
      </clipPath>
    </defs>
  );
}

function FlowToggle({ animate, setAnimate, reduced }: {
  animate: boolean; setAnimate: (v: boolean) => void; reduced: boolean;
}) {
  return (
    <button type="button" className="btn sm ghost"
      aria-pressed={animate} disabled={reduced}
      title={reduced ? "Flow animation off (reduced motion)" : animate ? "Pause call flow" : "Play call flow"}
      onClick={() => setAnimate(!animate)}>
      {animate ? <Pause size={13} aria-hidden /> : <Play size={13} aria-hidden />}
      {reduced ? "Flow off" : animate ? "Pause flow" : "Play flow"}
    </button>
  );
}

/* ── Per-API flow canvas (Flo decision-tree style) ─────────────────────
 * HTML card nodes on a pan/zoom canvas, orthogonal elbow connectors
 * with animated call flow, floating column
 * pills, and focus dimming: clicking a card keeps its whole flow
 * path lit, ghosts the rest, and opens the NodeInspector sidebar.
 * Double-clicking an API card recentres the flow on it. */

function egoTone(kind: string): FlowTone {
  if (kind === "CALLS_API") return "call";
  if (kind === "EXPOSES") return "meta";
  return "code";
}

const HEAD_H = 34; // room for the floating column pills
const VP_H = 470;

function kindDot(kind: GNode["kind"]): string {
  if (kind === "ApiEndpoint") return "call";
  if (kind === "ExternalApi") return "ext";
  if (kind === "Service") return "svc";
  return "code";
}

function nodeSub(node: GNode): string {
  if (node.kind === "ApiEndpoint")
    return `${node.service_id}${node.props.operation_id ? ` · ${node.props.operation_id}` : ""}`;
  if (node.kind === "ExternalApi") return node.props.host || "external";
  if (node.kind === "Service")
    return node.props.framework || node.props.language || node.repo_id;
  return `${node.service_id}${node.file ? ` · ${node.file.split("/").pop()}:${node.line_start}` : ""}`;
}

export function EgoGraph({ graph, openApi, go }: {
  graph: ApiGraph; openApi: (id: string) => void; go: (r: Route) => void;
}) {
  const reduced = usePrefersReducedMotion();
  const [playing, setPlaying] = useState(true);
  const [dragging, setDragging] = useState(false);
  const animate = playing && !reduced && !dragging && graph.nodes.length <= 250 && graph.edges.length <= 80;
  const [showHelpers, setShowHelpers] = useState(() => graph.nodes.filter(n => n.kind === "Symbol").length <= 40);
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const centerId = graph.endpoint?.id ?? "";
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const visibleNodes = useMemo(() => {
    if (showHelpers) return graph.nodes;
    return visibleGraphNodes(graph.nodes, graph.edges, centerId);
  }, [graph, centerId, showHelpers]);
  const edges = useMemo(
    () => egoEdges(graph.edges, centerId), [graph, centerId]);
  const { placed, width, height } = useMemo(
    () => layoutGraph(visibleNodes, edges, centerId),
    [visibleNodes, edges, centerId]);
  const pos = useMemo(() => new Map(placed.map((p) => [p.node.id, p])), [placed]);
  const flowPairs = useMemo(() => edges.flatMap((e) => {
    const pair = flowPair(e, pos);
    return pair ? [{ e, pair }] : [];
  }), [edges, pos]);
  const animatedEdges = useMemo(() => new Set([...flowPairs].sort((a, b) =>
    Number(["CALLS_API", "HANDLES"].includes(b.e.kind)) - Number(["CALLS_API", "HANDLES"].includes(a.e.kind))).slice(0, 36).map(p => p.e)), [flowPairs]);
  const focus = useMemo(() => selectedId
    ? focusSet(selectedId, flowPairs.map(({ pair }) => ({
        from_id: pair.from.node.id, to_id: pair.to.node.id })))
    : null, [selectedId, flowPairs]);
  const selected = selectedId
    ? graph.nodes.find((n) => n.id === selectedId) ?? null : null;
  const colHeads = useMemo(() => ["Callers", "This API", "Handler code", "Calls"]
    .map((h, i) => ({ h, i }))
    .filter(({ i }) => placed.some((p) => p.col === i)), [placed]);

  /* Selection never survives a recentre. */
  useEffect(() => {
    setSelectedId(null);
    setShowHelpers(graph.nodes.filter(n => n.kind === "Symbol").length <= 40);
  }, [centerId, graph]);
  useEffect(() => {
    if (!selectedId) return;
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") setSelectedId(null); };
    addEventListener("keydown", h);
    return () => removeEventListener("keydown", h);
  }, [selectedId]);

  /* ── pan / zoom ─────────────────────────────────────────────────── */
  const worldW = width;
  const worldH = height + HEAD_H;
  const vpRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const viewRef = useRef(view);

  const interacted = useRef(false);
  const dragMoved = useRef(false);
  const drag = useRef<{ px: number; py: number; vx: number; vy: number } | null>(null);

  const frame = useRef<number | null>(null);
  const scheduleView = useCallback((next: typeof view) => {
    viewRef.current = next;
    if (frame.current === null) frame.current = requestAnimationFrame(() => { frame.current = null; setView(viewRef.current); });
  }, []);
  useEffect(() => () => { if (frame.current !== null) cancelAnimationFrame(frame.current); frame.current = null; }, []);
  const fit = useCallback(() => {
    const el = vpRef.current;
    if (!el) return;
    const k = Math.max(0.65, Math.min(el.clientWidth / worldW, el.clientHeight / worldH, 1));
    const center = pos.get(centerId);
    scheduleView({ k, x: center ? el.clientWidth / 2 - (center.x + BOX_W / 2) * k : 0,
      y: center ? el.clientHeight / 2 - (center.y + HEAD_H + BOX_H / 2) * k : 0 });
  }, [worldW, worldH, pos, centerId, scheduleView]);
  useEffect(() => { interacted.current = false; fit(); }, [fit, centerId]);
  useEffect(() => {
    const el = vpRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      interacted.current = true;
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const py = e.clientY - rect.top;
      const v = viewRef.current;
      const k = Math.min(2, Math.max(0.3, v.k * Math.exp(-e.deltaY * 0.0015)));
      scheduleView({ k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    const ro = new ResizeObserver(() => { if (!interacted.current) fit(); });
    ro.observe(el);
    return () => { el.removeEventListener("wheel", onWheel); ro.disconnect(); };
  }, [fit, scheduleView]);
  const zoomBy = (f: number) => {
    interacted.current = true;
    const el = vpRef.current;
    const v = viewRef.current;
    const k = Math.min(2, Math.max(0.3, v.k * f));
    const cx = (el?.clientWidth ?? 0) / 2;
    const cy = (el?.clientHeight ?? 0) / 2;
    scheduleView({ k, x: cx - ((cx - v.x) / v.k) * k, y: cy - ((cy - v.y) / v.k) * k });
  };

  const onPointerDown = (e: React.PointerEvent) => {
    drag.current = { px: e.clientX, py: e.clientY, vx: viewRef.current.x, vy: viewRef.current.y };
    dragMoved.current = false;
    try { (e.target as HTMLElement).setPointerCapture?.(e.pointerId); } catch { /* synthetic pointer */ }
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.px;
    const dy = e.clientY - drag.current.py;
    if (Math.abs(dx) + Math.abs(dy) > 4) { dragMoved.current = true; interacted.current = true; setDragging(true); }
    if (dragMoved.current)
      scheduleView({ ...viewRef.current, x: drag.current!.vx + dx, y: drag.current!.vy + dy });
  };
  const onPointerUp = () => {
    drag.current = null;
    setDragging(false);
    window.setTimeout(() => { dragMoved.current = false; }, 0);
  };
  const pick = (id: string) => { if (!dragMoved.current) setSelectedId(id); };

  const layers = useMemo(() => <>
            <svg className="flow-edges" width={worldW} height={worldH}
              viewBox={`0 0 ${worldW} ${worldH}`} aria-hidden>
              <GraphDefs clipId={`flow-clip-${uid}`} w={BOX_W} h={BOX_H} rx={10} />
              {flowPairs.map(({ e, pair }, i) => {
                const from = { ...pair.from, y: pair.from.y + HEAD_H };
                const to = { ...pair.to, y: pair.to.y + HEAD_H };
                const d = pair.vertical ? elbowPathVertical(from, to) : elbowPath(from, to);
                const dim = focus !== null
                  && !(focus.has(pair.from.node.id) && focus.has(pair.to.node.id));
                return (
                  <g key={i} opacity={dim ? 0.12 : 1}>
                    <FlowEdge id={`fe-${uid}-${i}`} d={d}
                      tone={egoTone(e.kind)} animate={animate && !dim && animatedEdges.has(e)}
                      label={`${e.kind}: ${nodeLabel(pair.from.node)} → ${nodeLabel(pair.to.node)}`} />
                  </g>);
              })}
            </svg>
            {colHeads.map(({ h, i }) => (
              <span key={h} className="fcol-pill"
                style={{ left: placed.find(p => p.col === i)?.x ?? 16, top: 4 }}>{h}</span>
            ))}
            {placed.map(({ node, x, y }) => {
              const isCenter = node.id === centerId;
              const isSel = node.id === selectedId;
              const dim = focus !== null && !focus.has(node.id);
              return (
                <button key={node.id} type="button"
                  className={`fnode ${isCenter ? "center" : ""} ${isSel ? "selected" : ""} ${dim ? "dim" : ""}`}
                  style={{ left: x, top: y + HEAD_H, width: BOX_W, height: BOX_H }}
                  aria-pressed={isSel}
                  title={`${nodeLabel(node)} · ${node.kind}${node.file ? ` · ${node.file}:${node.line_start}` : ""}`}
                  onClick={() => pick(node.id)}
                  onDoubleClick={() => {
                    if (node.kind === "ApiEndpoint" && !isCenter) openApi(node.id);
                  }}>
                  <span className="fnode-r1">
                    <span className={`fnode-dot ${kindDot(node.kind)}`} aria-hidden />
                    <span className="fnode-title mono">{nodeLabel(node)}</span>
                    {node.props.deprecated === "true" && (
                      <span className="chip tone-off fnode-chip">deprecated</span>)}
                  </span>
                  <span className="fnode-sub">{nodeSub(node)}</span>
                </button>);
            })}
  </>, [worldW, worldH, uid, flowPairs, focus, animate, animatedEdges, placed, centerId, selectedId, openApi]);

  return (
    <div className="flow-shell">
      <div className="graphwrap flow-main">
        <div className="graphbar">
          <span className="dim t-sm">Drag to pan · scroll to zoom · click a node to inspect</span>
          <span className="spacer" />
          <span className="glegend dim t-sm">
            <span className="gdot-inline call" /> API call
            <span className="gdot-inline code" /> in-service code
            <span className="gdot-inline meta" /> exposes
          </span>
          <span className="row" style={{ gap: 2 }} role="group" aria-label="Zoom">
            <button type="button" className="iconbtn sm" aria-label="Zoom out"
              onClick={() => zoomBy(1 / 1.2)}><Minus size={13} aria-hidden /></button>
            <span className="dim mono t-sm" style={{ minWidth: 38, textAlign: "center" }}>
              {Math.round(view.k * 100)}%</span>
            <button type="button" className="iconbtn sm" aria-label="Zoom in"
              onClick={() => zoomBy(1.2)}><Plus size={13} aria-hidden /></button>
            <button type="button" className="iconbtn sm" aria-label="Fit to view"
              onClick={() => { interacted.current = false; fit(); }}>
              <Maximize size={13} aria-hidden /></button>
          </span>
          {graph.truncated && <span className="chip tone-risk">Partial graph · {graph.total_symbols ?? "?"} symbols · {graph.total_callers ?? "?"} callers</span>}
          <button className="btn sm ghost" aria-pressed={showHelpers} onClick={() => setShowHelpers(v => !v)}>{showHelpers ? "Hide" : "Show"} internal helpers</button>
          <FlowToggle animate={animate} setAnimate={setPlaying} reduced={reduced} />
        </div>
        <div ref={vpRef} className="flow-vp" style={{ height: VP_H }}
          role="application" aria-label="API flow canvas"
          onPointerDown={onPointerDown} onPointerMove={onPointerMove}
          onPointerUp={onPointerUp} onPointerLeave={onPointerUp}
          onClick={(e) => {
            if (!dragMoved.current && !(e.target as HTMLElement).closest(".fnode"))
              setSelectedId(null);
          }}>
          <div className="flow-world"
            style={{
              width: worldW, height: worldH,
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
            }}>
            {layers}
          </div>
        </div>
      </div>
      {selected && (
        <NodeInspector key={selected.id} node={selected} graph={graph}
          centerId={centerId} onClose={() => setSelectedId(null)}
          onSelect={setSelectedId} openApi={openApi} go={go} />
      )}
    </div>
  );
}
