/* Pure layout for the per-API ego graph.
 *
 * Columns read left to right as a request flows:
 *   0 callers (owner APIs + caller symbols)
 *   1 this API (and its service)
 *   2 handler code (handler + call closure)
 *   3 outbound APIs / external APIs
 *
 * Relative imports only + type-only references, so node --test can load
 * this file directly (same rule as Arc's platform/src/lib).
 */
import type { GEdge, GNode } from "./types.ts";

export interface Placed {
  node: GNode;
  col: number;
  x: number;
  y: number;
}

export const COL_W = 272;
export const ROW_H = 80;
export const BOX_W = 228;
export const BOX_H = 56;

/** End-truncate with an ellipsis to at most `max` characters. */
export function truncateEnd(s: string, max: number): string {
  if (max < 2) return s.slice(0, Math.max(0, max));
  return s.length <= max ? s : s.slice(0, max - 1) + "…";
}

/** Middle-truncate to at most `max` characters, keeping the head and
 *  the tail — paths stay recognisable at both ends
 *  ("/checkout/{or…}/status"). */
export function truncateMiddle(s: string, max: number): string {
  if (s.length <= max) return s;
  if (max < 6) return truncateEnd(s, max);
  const head = Math.ceil((max - 1) * 0.6);
  const tail = (max - 1) - head;
  return s.slice(0, head) + "…" + s.slice(s.length - tail);
}

/** Edges worth drawing in an ego graph. The linker can emit a
 *  CALLS_API self-edge for the handler symbol (it also HANDLES the
 *  endpoint) plus a HANDLES_CHAIN from the endpoint to its own
 *  handler; both draw as noise on top of the real HANDLES edge, and
 *  exact duplicates stack packets. Returns a cleaned, deduped list. */
export function egoEdges(edges: GEdge[], centerId: string): GEdge[] {
  const handlers = new Set(
    edges.filter((e) => e.kind === "HANDLES" && e.to_id === centerId)
      .map((e) => e.from_id));
  const seen = new Set<string>();
  const out: GEdge[] = [];
  for (const e of edges) {
    if (e.kind === "CALLS_API" && e.to_id === centerId && handlers.has(e.from_id)) continue;
    if (e.kind === "HANDLES_CHAIN" && e.from_id === centerId) continue;
    const key = `${e.from_id}→${e.to_id}→${e.kind}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out;
}

export function columnOf(
  node: GNode,
  centerId: string,
  edges: GEdge[],
): number {
  if (node.id === centerId || node.kind === "Service") return 1;
  // The handler wins over "caller": it may also carry a CALLS_API
  // self-edge (see egoEdges), but it is handler code, not a caller.
  const handlesCenter = edges.some(
    (e) => e.from_id === node.id && e.to_id === centerId && e.kind === "HANDLES",
  );
  if (handlesCenter) return 2;
  const callsCenter = edges.some(
    (e) => e.from_id === node.id && e.to_id === centerId && e.kind === "CALLS_API",
  );
  if (callsCenter) return 0;
  const ownsCaller = edges.some(
    (e) => e.from_id === node.id && e.kind === "HANDLES_CHAIN",
  );
  if (ownsCaller) return 0;
  if (node.kind === "ApiEndpoint" || node.kind === "ExternalApi") return 3;
  return 2;
}

export function layoutGraph(
  nodes: GNode[],
  edges: GEdge[],
  centerId: string,
): { placed: Placed[]; width: number; height: number } {
  const byCol = new Map<number, GNode[]>();
  for (const n of nodes) {
    const col = columnOf(n, centerId, edges);
    const arr = byCol.get(col) ?? [];
    arr.push(n);
    byCol.set(col, arr);
  }
  const placed: Placed[] = [];
  let maxRows = 1;
  for (const [col, arr] of [...byCol.entries()].sort((a, b) => a[0] - b[0])) {
    arr.sort((a, b) => a.id.localeCompare(b.id));
    maxRows = Math.max(maxRows, arr.length);
    arr.forEach((node, i) => {
      placed.push({ node, col, x: 16 + col * COL_W, y: 16 + i * ROW_H });
    });
  }
  return {
    placed,
    width: 32 + 3 * COL_W + BOX_W,
    height: 32 + maxRows * ROW_H,
  };
}

/** Orthogonal elbow connector (Flo decision-tree style): leaves the
 *  source's right edge perpendicular, turns with rounded corners on a
 *  shared vertical spine, and enters the target's left edge. Falls
 *  back to a straight line when both centres share a row. */
export function elbowPath(a: Placed, b: Placed): string {
  const x1 = a.x + BOX_W;
  const y1 = a.y + BOX_H / 2;
  const x2 = b.x;
  const y2 = b.y + BOX_H / 2;
  if (y1 === y2 || x2 <= x1) return `M ${x1} ${y1} L ${x2} ${y2}`;
  const mx = x1 + Math.max(18, (x2 - x1) / 2);
  const dir = y2 > y1 ? 1 : -1;
  const r = Math.min(10, Math.abs(y2 - y1) / 2, (x2 - x1) / 4);
  return `M ${x1} ${y1} L ${mx - r} ${y1} Q ${mx} ${y1} ${mx} ${y1 + dir * r} `
    + `L ${mx} ${y2 - dir * r} Q ${mx} ${y2} ${mx + r} ${y2} L ${x2} ${y2}`;
}

/** Same-column connector routed on a spine just left of the column,
 *  so it never crosses intervening cards: out of the source's left
 *  edge, vertically along the spine, into the target's left edge. */
export function elbowPathVertical(a: Placed, b: Placed): string {
  const y1 = a.y + BOX_H / 2;
  const y2 = b.y + BOX_H / 2;
  const sx = a.x - 14;
  if (y1 === y2) return `M ${a.x} ${y1} L ${b.x} ${y2}`;
  const dir = y2 > y1 ? 1 : -1;
  const r = Math.min(8, Math.abs(y2 - y1) / 2);
  return `M ${a.x} ${y1} L ${sx + r} ${y1} Q ${sx} ${y1} ${sx} ${y1 + dir * r} `
    + `L ${sx} ${y2 - dir * r} Q ${sx} ${y2} ${sx + r} ${y2} L ${b.x} ${y2}`;
}

/** Nodes on the selected node's flow path: itself plus everything
 *  upstream (can reach it) and downstream (reachable from it) over
 *  flow-direction pairs. Everything else is dimmed (Flo focus). */
export function focusSet(
  selectedId: string,
  pairs: { from_id: string; to_id: string }[],
): Set<string> {
  const down = new Map<string, string[]>();
  const up = new Map<string, string[]>();
  for (const p of pairs) {
    down.set(p.from_id, [...(down.get(p.from_id) ?? []), p.to_id]);
    up.set(p.to_id, [...(up.get(p.to_id) ?? []), p.from_id]);
  }
  const keep = new Set([selectedId]);
  const walk = (next: Map<string, string[]>, start: string) => {
    const queue = [start];
    while (queue.length) {
      const cur = queue.pop()!;
      for (const n of next.get(cur) ?? []) {
        if (!keep.has(n)) { keep.add(n); queue.push(n); }
      }
    }
  };
  walk(down, selectedId);
  walk(up, selectedId);
  return keep;
}

export function edgePath(a: Placed, b: Placed): string {
  const x1 = a.x + BOX_W;
  const y1 = a.y + BOX_H / 2;
  const x2 = b.x;
  const y2 = b.y + BOX_H / 2;
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

/** Vertical connector for same-column pairs (service EXPOSES its API,
 *  owner API HANDLES_CHAIN its caller symbol). Works in either
 *  vertical order: EXPOSES is stored service -> endpoint while the
 *  service box usually sits below its endpoint in the same column. */
export function edgePathVertical(a: Placed, b: Placed): string {
  const x = a.x + BOX_W / 2;
  const y1 = a.y < b.y ? a.y + BOX_H : a.y;
  const y2 = a.y < b.y ? b.y : b.y + BOX_H;
  const my = (y1 + y2) / 2;
  return `M ${x} ${y1} C ${x} ${my}, ${x} ${my}, ${x} ${y2}`;
}

/** Visual flow direction is request direction (left to right), which is
 *  not always the stored edge direction: HANDLES is stored
 *  handler -> endpoint (col 2 -> col 1) but a request flows
 *  endpoint -> handler. Returns the pair in flow order. */
export function flowPair(
  edge: GEdge,
  pos: Map<string, Placed>,
): { from: Placed; to: Placed; vertical: boolean } | null {
  const a = pos.get(edge.from_id);
  const b = pos.get(edge.to_id);
  if (!a || !b || a.node.id === b.node.id) return null;
  // EXPOSES is not a call: keep its stored service -> endpoint
  // direction (the arrow says who exposes whom, not request flow).
  if (edge.kind === "EXPOSES") return { from: a, to: b, vertical: a.col === b.col };
  if (a.col !== b.col) {
    return a.col < b.col
      ? { from: a, to: b, vertical: false }
      : { from: b, to: a, vertical: false };
  }
  // Same column (in-service CALLS, HANDLES_CHAIN): the stored
  // direction is the call direction, whichever row sorts first.
  return { from: a, to: b, vertical: true };
}
