/* Ask usage graph — on-demand, one slot per answer.
 * Tier 1 derives instantly from rows already in the answer (no fetch).
 * Tier 2 lazily fetches one endpoint's full ego graph only on request. */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ApiGraph, AskGraphData, AskGraphNode, Citation, Route } from "@base/types";
import { EgoGraph } from "./graph";

export interface AskGraphLayout { placed: { node: AskGraphNode; x: number; y: number; col: number }[]; width: number; height: number }

const BOX_W = 232, BOX_H = 48;

export function AskResultGraph({ full, data, layout, rowCount, citations = [], expanded, open, onToggle, selectedId, onSelect, openCitation, openApi, go, fetchGraph }: {
  full: AskGraphData; data: AskGraphData; layout: AskGraphLayout; rowCount: number; citations?: Citation[]; expanded: boolean;
  open: boolean; onToggle: () => void;
  selectedId: string | null; onSelect: (id: string | null) => void;
  openCitation: (c: Citation) => void; openApi: (id: string) => void; go: (r: Route) => void;
  fetchGraph: (id: string) => Promise<ApiGraph>;
}) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const endpointCitations = useMemo(() => citations.filter(c => c.node_id.startsWith("endpoint::")), [citations]);
  const [drillId, setDrillId] = useState<string | null>(null);
  const [drillGraph, setDrillGraph] = useState<ApiGraph | null>(null);
  const [drillError, setDrillError] = useState("");
  const [loading, setLoading] = useState(false);
  const cache = useRef(new Map<string, ApiGraph>());
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);

  if (!full.graphable && !endpointCitations.length) return null;

  const totals = full.totals;
  const label = full.graphable
    ? `Show usage graph · ${totals.apis} API${totals.apis === 1 ? "" : "s"} · ${totals.calls} call${totals.calls === 1 ? "" : "s"}`
    : `Show usage graph · ${endpointCitations.length} API${endpointCitations.length === 1 ? "" : "s"}`;

  const drill = async (id: string) => {
    setDrillId(id);
    setDrillError("");
    const hit = cache.current.get(id);
    if (hit) { setDrillGraph(hit); return; }
    setDrillGraph(null); setLoading(true);
    try {
      const g = await fetchGraph(id);
      if (!live.current) return;
      cache.current.set(id, g); setDrillGraph(g);
    } catch (e) { if (live.current) setDrillError(String(e)); }
    finally { if (live.current) setLoading(false); }
  };

  const pos = new Map(layout.placed.map(p => [p.node.id, p]));
  const selectedNode = data.nodes.find(n => n.id === selectedId) ?? null;

  return <div className="ask-graph">
    <button type="button" className="ask-show ask-graph-toggle" aria-expanded={open} onClick={onToggle}>
      {open ? "Hide usage graph" : label}
    </button>
    {open && <div className="ask-graph-slot">
      {drillId ? <>
        <div className="ask-graph-bar">
          <button type="button" className="btn sm ghost" onClick={() => setDrillId(null)}>← Back to result graph</button>
          <button type="button" className="btn sm" onClick={() => openApi(drillId)}>Open API</button>
        </div>
        {loading && <p className="ask-graph-note" role="status">Loading graph…</p>}
        {drillError && <p className="ask-graph-note"><span className="ask-attention">Graph failed: {drillError}</span>{" "}
          <button type="button" className="ask-show" onClick={() => void drill(drillId)}>Retry</button></p>}
        {drillGraph && <EgoGraph graph={drillGraph} openApi={openApi} go={go} embedded height={320} />}
      </> : full.graphable ? <>
        {data.aggregated && <p className="ask-graph-note">Aggregated by service — {totals.apis} APIs · {totals.calls} calls in this answer. Expand the list or open one API for detail.</p>}
        {!expanded && rowCount > 8 && <p className="ask-graph-note">Showing first 8 rows — “Show all” expands the graph too.</p>}
        <div className="ask-graph-scroll">
          <div className="ask-graph-canvas" style={{ width: layout.width, height: layout.height }}>
            <svg className="ask-graph-edges" width={layout.width} height={layout.height} aria-hidden>
              <defs><marker id={`ag-arrow-${uid}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 1 L 10 5 L 0 9 z" style={{ fill: "var(--tx-3)" }} /></marker></defs>
              {data.edges.map((e, i) => {
                const a = pos.get(e.from), b = pos.get(e.to); if (!a || !b) return null;
                const x1 = a.x + BOX_W, y1 = a.y + BOX_H / 2, x2 = b.x, y2 = b.y + BOX_H / 2, mx = (x1 + x2) / 2;
                const dim = selectedId && e.from !== selectedId && e.to !== selectedId;
                return <g key={i} opacity={dim ? 0.15 : 1}>
                  <path d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`} className={`gedge-base ask-graph-edge ${e.kind}`} fill="none" markerEnd={`url(#ag-arrow-${uid})`}>
                    <title>{e.kind === "include" ? "included in" : "calls"}{e.viaLabel ? ` via ${e.viaLabel}` : ""}{e.rule ? ` · ${e.rule}` : ""}</title>
                  </path>
                  {e.count > 1 && <text x={mx} y={(y1 + y2) / 2 - 4} className="ask-graph-count" textAnchor="middle">×{e.count}</text>}
                </g>;
              })}
            </svg>
            {layout.placed.map(({ node, x, y }) => {
              const dim = selectedId && node.id !== selectedId && !data.edges.some(e => (e.from === selectedId && e.to === node.id) || (e.to === selectedId && e.from === node.id));
              return <button key={node.id} type="button"
                className={`fnode ask-graph-node ${node.id === selectedId ? "selected" : ""} ${dim ? "dim" : ""}`}
                style={{ left: x, top: y, width: BOX_W, height: BOX_H }}
                aria-pressed={node.id === selectedId}
                title={`${node.label}${node.sub ? ` · ${node.sub}` : ""}`}
                onClick={() => onSelect(node.id === selectedId ? null : node.id)}>
                <span className="fnode-r1"><span className={`fnode-dot ${node.kind === "endpoint" ? "call" : node.kind === "service" ? "svc" : node.kind === "external" ? "ext" : "code"}`} aria-hidden />
                  <span className="fnode-title mono">{node.label}</span></span>
                {node.sub && <span className="fnode-sub">{node.sub}</span>}
              </button>;
            })}
          </div>
        </div>
        {selectedNode && <div className="ask-graph-bar">
          <span className="ask-graph-note">{selectedNode.label}</span>
          {selectedNode.citation && selectedNode.kind === "endpoint" ? <>
            <button type="button" className="btn sm primary" onClick={() => void drill(selectedNode.id)}>View full graph</button>
            <button type="button" className="btn sm ghost" onClick={() => openCitation(selectedNode.citation!)}>Open API</button>
          </> : null}
          {selectedNode.citation && selectedNode.kind !== "endpoint" ? <button type="button" className="btn sm ghost" onClick={() => openCitation(selectedNode.citation!)}>Open</button> : null}
        </div>}
      </> : <>
        <p className="ask-graph-note">No call edges in this answer. Pick an API for its full graph:</p>
        <div className="ask-graph-pick">{endpointCitations.map(c => <button key={c.node_id} type="button" className="btn sm ghost" onClick={() => void drill(c.node_id)}>{c.label}</button>)}</div>
      </>}
    </div>}
  </div>;
}
