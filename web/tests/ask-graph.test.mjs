import test from 'node:test';
import assert from 'node:assert/strict';
import { buildAskGraph, layoutAskGraph, ASK_GRAPH_NODE_LIMIT } from '../src/lib/ask-graph.ts';

const ep = (svc, method, path) => ({ node_id: `endpoint::r::${svc}::${method}:${path}`, label: `${method} ${path}`, file: '', line: 0 });
const row = (s, t, via = null) => ({ source: s, target: t, via, rule: 'call', confidence: 'high' });

test('empty and citation-only rows are not graphable', () => {
  assert.equal(buildAskGraph([]).graphable, false);
  assert.equal(buildAskGraph([row(ep('a', 'GET', '/x'), null)]).graphable, false);
});

test('call rows become call edges with totals', () => {
  const g = buildAskGraph([row(ep('a', 'GET', '/x'), ep('b', 'POST', '/y')), row(ep('a', 'GET', '/x'), ep('b', 'POST', '/y'))]);
  assert.equal(g.graphable, true);
  assert.equal(g.aggregated, false);
  assert.equal(g.nodes.length, 2);
  assert.equal(g.edges.length, 1);
  assert.equal(g.edges[0].count, 2);
  assert.deepEqual(g.totals, { apis: 2, calls: 2, services: 2, rows: 2 });
});

test('via-only rows become inclusion edges, not calls', () => {
  const sym = { node_id: 'symbol::r::f.py::price_cart', label: 'price_cart', file: 'f.py', line: 3 };
  const g = buildAskGraph([{ source: ep('a', 'GET', '/x'), target: null, via: sym, rule: '', confidence: null }]);
  assert.equal(g.edges[0].kind, 'include');
  assert.equal(g.edges[0].from, sym.node_id);
  assert.equal(g.totals.calls, 0);
});

test('layout puts targets right of sources', () => {
  const g = buildAskGraph([row(ep('a', 'GET', '/x'), ep('b', 'POST', '/y'))]);
  const { placed } = layoutAskGraph(g);
  const a = placed.find(p => p.node.id.includes('/x')), b = placed.find(p => p.node.id.includes('/y'));
  assert.ok(b.x > a.x);
});

test('large answers aggregate to services with honest counts', () => {
  const rows = [];
  for (let i = 0; i < ASK_GRAPH_NODE_LIMIT + 5; i++) rows.push(row(ep(`svc${i % 3}`, 'GET', `/x${i}`), ep(`svc${(i + 1) % 3}`, 'GET', `/y${i}`)));
  const g = buildAskGraph(rows);
  assert.equal(g.aggregated, true);
  assert.ok(g.nodes.every(n => n.kind === 'service'));
  assert.equal(g.totals.apis, 58);
  assert.equal(g.edges.reduce((s, e) => s + e.count, 0), rows.length);
});
