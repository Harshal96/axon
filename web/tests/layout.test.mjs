import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BOX_H, BOX_W, columnOf, egoEdges, elbowPath, elbowPathVertical, flowPair,
  focusSet, layoutGraph, truncateEnd, truncateMiddle,
} from "../base/layout.ts";
import { serviceRows } from "../base/catalog.ts";
import { DEMO_APIS, DEMO_SERVICES } from "../src/lib/demo.ts";

const node = (id, kind, service_id = "svc") => ({
  id, kind, name: id, repo_id: "r", service_id, file: "", line_start: 0,
  line_end: 0, props: {},
});

test("columns flow callers -> api -> code -> outbound", () => {
  const center = node("endpoint::c", "ApiEndpoint");
  const caller = node("symbol::caller", "Symbol");
  const handler = node("symbol::handler", "Symbol");
  const out = node("endpoint::out", "ApiEndpoint", "other");
  const edges = [
    { from_id: caller.id, to_id: center.id, kind: "CALLS_API" },
    { from_id: handler.id, to_id: center.id, kind: "HANDLES" },
    // Linker noise seen live: the handler also CALLS_API its own
    // endpoint (twice) and the endpoint HANDLES_CHAINs back to it.
    { from_id: handler.id, to_id: center.id, kind: "CALLS_API" },
    { from_id: handler.id, to_id: center.id, kind: "CALLS_API" },
    { from_id: center.id, to_id: handler.id, kind: "HANDLES_CHAIN" },
    { from_id: handler.id, to_id: out.id, kind: "CALLS_API" },
  ];
  assert.equal(columnOf(caller, center.id, edges), 0);
  assert.equal(columnOf(center, center.id, edges), 1);
  assert.equal(columnOf(handler, center.id, edges), 2);
  assert.equal(columnOf(out, center.id, edges), 3);
  const { placed, width, height } = layoutGraph(
    [center, caller, handler, out], edges, center.id);
  assert.equal(placed.length, 4);
  assert.ok(width > 0 && height > 0);
  assert.ok(placed.find((p) => p.node.id === caller.id).x
    < placed.find((p) => p.node.id === out.id).x);
  // Cleaned edges: handler self-edges and duplicates gone, real
  // caller + handler + outbound edges kept.
  const clean = egoEdges(edges, center.id);
  assert.deepEqual(clean.map((e) => e.kind).sort(),
    ["CALLS_API", "CALLS_API", "HANDLES"]);
  assert.ok(!clean.some((e) => e.from_id === handler.id
    && e.to_id === center.id && e.kind === "CALLS_API"));
});

test("flow direction is request direction, not stored HANDLES direction", () => {
  const center = node("endpoint::c", "ApiEndpoint");
  const handler = node("symbol::handler", "Symbol");
  const edges = [{ from_id: handler.id, to_id: center.id, kind: "HANDLES" }];
  const { placed } = layoutGraph([center, handler], edges, center.id);
  const pos = new Map(placed.map((p) => [p.node.id, p]));
  const pair = flowPair(edges[0], pos);
  // HANDLES is stored handler -> endpoint, but a request flows
  // endpoint -> handler, so packets must travel that way.
  assert.equal(pair.from.node.id, center.id);
  assert.equal(pair.to.node.id, handler.id);
  assert.equal(pair.vertical, false);
});

test("elbow connectors leave/enter perpendicular (decision-tree style)", () => {
  const a = { node: node("a", "ApiEndpoint"), col: 1, x: 100, y: 0 };
  const b = { node: node("b", "Symbol"), col: 2, x: 372, y: 80 };
  const d = elbowPath(a, b);
  // Starts at a's right-centre heading right, ends at b's left-centre.
  assert.ok(d.startsWith(`M ${100 + BOX_W} ${BOX_H / 2} L `), d);
  assert.ok(d.endsWith(`L 372 ${80 + BOX_H / 2}`), d);
  assert.ok(d.includes(" Q "), "rounded corners");
  // The vertical spine is strictly between the two boxes.
  const spineX = Number(d.match(/L ([\d.]+) \d/)?.[1]) + 10;
  assert.ok(spineX > 100 + BOX_W - 10 && spineX < 372, `spine ${spineX}`);
  // Same-column spine sits left of the cards, never through them.
  const v = elbowPathVertical(a, { ...b, col: 1, x: 100, y: 160 });
  assert.ok(v.startsWith("M 100 "), v);
  assert.ok(v.includes(" 86 "), `spine at x-14: ${v}`);
});

test("focus set keeps the selected node's whole flow path", () => {
  const pairs = [
    { from_id: "caller", to_id: "api" },
    { from_id: "api", to_id: "handler" },
    { from_id: "handler", to_id: "out" },
    { from_id: "other", to_id: "api" },
    { from_id: "handler", to_id: "side" },
  ];
  const keep = focusSet("handler", pairs);
  // Upstream callers and downstream calls stay; nothing else exists
  // off-path here, so add an unrelated pair and re-check.
  assert.deepEqual([...keep].sort(), ["api", "caller", "handler", "other", "out", "side"]);
  const keep2 = focusSet("out", [...pairs, { from_id: "x", to_id: "y" }]);
  assert.ok(!keep2.has("x") && !keep2.has("y") && keep2.has("caller"));
});

test("graph labels are fitted inside their boxes", () => {
  // The exact labels that overflowed a 200px box at 12px mono.
  const title = truncateMiddle("GET /checkout/{order_id}/status", 30);
  assert.ok(title.length <= 30);
  assert.ok(title.startsWith("GET /checkout/") && title.endsWith("/status"));
  assert.ok(title.includes("…"));
  // 30 chars of 11px mono (~6.6px/char) + 10px left padding fits the box.
  assert.ok(30 * 6.6 + 10 <= BOX_W, `${BOX_W}`);
  assert.equal(truncateMiddle("GET /v1/charges/{charge_id}", 30),
    "GET /v1/charges/{charge_id}"); // already fits — untouched
  assert.equal(truncateEnd("ApiEndpoint · checkout", 36), "ApiEndpoint · checkout");
  assert.ok(truncateEnd("a".repeat(60), 36).length === 36);
  assert.ok(truncateMiddle("a".repeat(60), 30).length === 30);
});

test("service rows count APIs and deprecated from the lists alone", () => {
  const rows = serviceRows(DEMO_SERVICES, DEMO_APIS);
  assert.equal(rows.length, 3);
  const checkout = rows.find((r) => r.service.service_id === "checkout");
  const payment = rows.find((r) => r.service.service_id === "payment");
  assert.ok(checkout.apis >= 1);
  assert.ok(payment.apis >= 1);
  const legacy = rows.filter((r) => r.deprecated > 0);
  assert.equal(legacy.length >= 1, true);
  // Sorted by service id, and no whole-graph fields remain.
  assert.deepEqual(rows.map((r) => r.service.service_id),
    [...rows.map((r) => r.service.service_id)].sort());
  assert.equal("callsOut" in rows[0], false);
});
