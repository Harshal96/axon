/* Pure derivation: AskResponse rows -> a small usage graph.
 * No fetch, no DOM. Relative imports only so node --test loads it.
 * Edges are never invented: a call edge exists only when a row has a
 * target, an inclusion edge only when a row has a via symbol and no
 * target (function -> APIs). Citation-only answers stay ungraphable
 * here; they get per-endpoint drill-down only in the UI. */
import type { AnswerRow, AskGraphData, AskGraphEdge, AskGraphNode, AskGraphNodeKind, Citation } from "../../base/types.ts";
import { citationParts } from "./ask-format.ts";

export type { AskGraphData, AskGraphEdge, AskGraphNode, AskGraphNodeKind } from "../../base/types.ts";
export const ASK_GRAPH_NODE_LIMIT = 24;
export const ASK_GRAPH_EDGE_LIMIT = 40;

function nodeKind(id: string): AskGraphNodeKind {
  if (id.startsWith("endpoint::")) return "endpoint";
  if (id.startsWith("external::")) return "external";
  if (id.startsWith("service::")) return "service";
  return "symbol";
}

function toNode(c: Citation): AskGraphNode {
  const kind = nodeKind(c.node_id);
  if (kind === "endpoint" || kind === "external") {
    const p = citationParts(c);
    return { id: c.node_id, kind, label: p.method ? `${p.method} ${p.path}` : p.path, sub: p.service, service: p.service, citation: c };
  }
  if (kind === "service") {
    const service = c.node_id.split("::").pop() ?? c.label;
    return { id: c.node_id, kind, label: service, sub: "service", service, citation: c };
  }
  return { id: c.node_id, kind, label: c.label, sub: c.file ? `${c.file}:${c.line}` : "", service: "", citation: c };
}

const EMPTY: AskGraphData = { graphable: false, aggregated: false, nodes: [], edges: [], totals: { apis: 0, calls: 0, services: 0, rows: 0 } };

export function buildAskGraph(rows: AnswerRow[]): AskGraphData {
  if (!rows?.length) return { ...EMPTY, totals: { ...EMPTY.totals } };
  const nodes = new Map<string, AskGraphNode>();
  const edgeMap = new Map<string, AskGraphEdge>();
  const add = (c: Citation) => { if (!nodes.has(c.node_id)) nodes.set(c.node_id, toNode(c)); };
  for (const row of rows) {
    add(row.source);
    if (row.target) {
      add(row.target);
      const key = `call|${row.source.node_id}|${row.target.node_id}`;
      const prev = edgeMap.get(key);
      if (prev) prev.count += 1;
      else edgeMap.set(key, { from: row.source.node_id, to: row.target.node_id, kind: "call", count: 1, viaLabel: row.via?.label ?? "", rule: row.rule ?? "", confidence: row.confidence ?? "" });
    } else if (row.via) {
      add(row.via);
      const key = `include|${row.via.node_id}|${row.source.node_id}`;
      const prev = edgeMap.get(key);
      if (prev) prev.count += 1;
      else edgeMap.set(key, { from: row.via.node_id, to: row.source.node_id, kind: "include", count: 1, viaLabel: "", rule: row.rule ?? "", confidence: row.confidence ?? "" });
    }
  }
  let edges = [...edgeMap.values()];
  const apiNodes = [...nodes.values()].filter(n => n.kind === "endpoint" || n.kind === "external");
  const totals = {
    apis: apiNodes.length,
    calls: edges.filter(e => e.kind === "call").reduce((s, e) => s + e.count, 0),
    services: new Set(apiNodes.map(n => n.service).filter(Boolean)).size,
    rows: rows.length,
  };
  if (!edges.length) return { graphable: false, aggregated: false, nodes: [...nodes.values()], edges: [], totals };

  let outNodes = [...nodes.values()];
  let aggregated = false;
  if (outNodes.length > ASK_GRAPH_NODE_LIMIT || edges.length > ASK_GRAPH_EDGE_LIMIT) {
    aggregated = true;
    const svcId = (n: AskGraphNode) => `service::${n.service || "unknown"}`;
    const remap = new Map<string, AskGraphNode>();
    for (const n of outNodes) {
      const agg = (n.kind === "endpoint" || n.kind === "external")
        ? { id: svcId(n), kind: "service" as const, label: n.service || "unknown", sub: "service", service: n.service, citation: null }
        : n;
      remap.set(n.id, agg);
    }
    const aggNodes = new Map<string, AskGraphNode>();
    for (const n of remap.values()) aggNodes.set(n.id, n);
    const aggEdges = new Map<string, AskGraphEdge>();
    for (const e of edges) {
      const from = remap.get(e.from)!, to = remap.get(e.to)!;
      if (from.id === to.id) continue;
      const key = `${e.kind}|${from.id}|${to.id}`;
      const prev = aggEdges.get(key);
      if (prev) prev.count += e.count;
      else aggEdges.set(key, { ...e, from: from.id, to: to.id });
    }
    outNodes = [...aggNodes.values()];
    edges = [...aggEdges.values()];
    if (!edges.length) return { graphable: false, aggregated, nodes: outNodes, edges: [], totals };
  }
  return { graphable: true, aggregated, nodes: outNodes, edges, totals };
}

/** Column layout by longest-path depth (cycles bounded by node count). */
export function layoutAskGraph(data: AskGraphData): {
  placed: { node: AskGraphNode; x: number; y: number; col: number }[];
  width: number; height: number;
} {
  const COL_W = 264, ROW_H = 64, BOX_W = 232;
  const depth = new Map(data.nodes.map(n => [n.id, 0]));
  for (let i = 0; i < data.nodes.length; i++) {
    let changed = false;
    for (const e of data.edges) {
      const d = (depth.get(e.from) ?? 0) + 1;
      if (d > (depth.get(e.to) ?? 0) && d <= data.nodes.length) { depth.set(e.to, d); changed = true; }
    }
    if (!changed) break;
  }
  const byCol = new Map<number, AskGraphNode[]>();
  for (const n of data.nodes) { const c = depth.get(n.id) ?? 0; const a = byCol.get(c) ?? []; a.push(n); byCol.set(c, a); }
  const placed: { node: AskGraphNode; x: number; y: number; col: number }[] = [];
  let maxRows = 1;
  for (const [col, arr] of [...byCol.entries()].sort((a, b) => a[0] - b[0])) {
    maxRows = Math.max(maxRows, arr.length);
    arr.forEach((node, i) => placed.push({ node, col, x: 8 + col * COL_W, y: 8 + i * ROW_H }));
  }
  const cols = byCol.size || 1;
  return { placed, width: 8 + (cols - 1) * COL_W + BOX_W + 8, height: 8 + maxRows * ROW_H + 8 };
}
