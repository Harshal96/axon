/** Function catalog over the same evidence graph as the demo API canvas. */
import type { Facet, FunctionDetail, FunctionOwner, FunctionQuery, FunctionRow, FunctionsPage, GEdge, GNode } from "../../base/types.ts";
import { DEMO_APIS, DEMO_SYMBOLS, demoGraph } from "./demo.ts";

function owner(symbol: GNode): FunctionOwner {
  const kind = symbol.props.owner_kind || (symbol.file.startsWith("clients/") ? "client" : symbol.service_id ? "service" : "unowned");
  const id = kind === "shared" ? symbol.props.owner_root : symbol.service_id || symbol.file.split("/")[0];
  return { key: `${symbol.repo_id}/${kind}/${id}`, kind, id, label: `${kind} · ${id}` };
}
const graphs = DEMO_APIS.map(api => demoGraph(api.id));
const edges = [...new Map(graphs.flatMap(g => g.edges).map(e => [`${e.from_id}/${e.to_id}/${e.kind}`, e])).values()];
const calls = edges.filter(e => e.kind === "CALLS");
const byId = new Map(DEMO_SYMBOLS.map(n => [n.id, n]));
function pathsTo(symbol: GNode): FunctionDetail["apis"] {
  return DEMO_APIS.flatMap(endpoint => {
    const handler = edges.find(e => e.kind === "HANDLES" && e.to_id === endpoint.id)?.from_id;
    if (!handler || !byId.has(handler)) return [];
    const queue: string[][] = [[handler]];
    const seen = new Set([handler]);
    while (queue.length) {
      const path = queue.shift()!;
      if (path.at(-1) === symbol.id) return [{ endpoint, path: path.map(id => byId.get(id)!), depth: path.length - 1,
        confidence: "EXTRACTED", edges: path.slice(1).map((to_id, i) => ({ from_id: path[i], to_id, kind: "CALLS", confidence: "EXTRACTED", evidence: { file: byId.get(path[i])!.file, line: 5, rule: "call:demo" } })) }];
      if (path.length > 12) continue;
      for (const edge of calls.filter(e => e.from_id === path.at(-1))) {
        if (seen.has(edge.to_id) || !byId.has(edge.to_id)) continue;
        seen.add(edge.to_id); queue.push([...path, edge.to_id]);
      }
    }
    return [];
  });
}
const allRows: FunctionRow[] = DEMO_SYMBOLS.map(symbol => {
  const paths = pathsTo(symbol);
  const services = new Map<string, { key: string; repo_id: string; service_id: string; api_count: number }>();
  for (const { endpoint } of paths) {
    const key = `${endpoint.repo_id}/${endpoint.service_id}`;
    const use = services.get(key) || { key, repo_id: endpoint.repo_id, service_id: endpoint.service_id, api_count: 0 };
    use.api_count++; services.set(key, use);
  }
  return { symbol, owner: owner(symbol), package: symbol.file.replace(/\.py$/, "").replaceAll("/", "."),
    api_count: paths.length, service_count: services.size, used_in: [...services.values()],
    caller_count: new Set(calls.filter(e => e.to_id === symbol.id).map(e => e.from_id)).size,
    is_handler: edges.some(e => e.from_id === symbol.id && e.kind === "HANDLES"), same_name_count: 1,
    min_depth: paths.length ? Math.min(...paths.map(p => p.depth)) : null };
});
export function demoFunctions(query: FunctionQuery): FunctionsPage {
  const q = (query.q || "").trim().toLowerCase();
  const candidates = allRows.filter(r => (!query.repo || query.repo === r.symbol.repo_id)
    && (q.length < 2 || `${r.symbol.name} ${r.symbol.props.qualified}`.toLowerCase().includes(q))
    && (!query.hide_handlers || !r.is_handler));
  const scoped = candidates.map(row => {
    const usage = row.used_in.filter(u => !query.used_in?.length || query.used_in.includes(u.key));
    const paths = pathsTo(row.symbol).filter(p => !query.used_in?.length || query.used_in.includes(`${p.endpoint.repo_id}/${p.endpoint.service_id}`));
    return { ...row, used_in: usage, api_count: usage.reduce((sum, u) => sum + u.api_count, 0), service_count: usage.length,
      min_depth: paths.length ? Math.min(...paths.map(p => p.depth)) : null };
  });
  const matches = (row: FunctionRow, except = "") =>
    (except === "used_in" || !query.used_in?.length || row.api_count > 0)
    && (except === "defined_in" || !query.defined_in?.length || query.defined_in.includes(row.owner.key))
    && (except === "kind" || !query.kind?.length || query.kind.includes((row.symbol.props.kind || "function").toLowerCase()))
    && (!query.unreached || allRows.find(r => r.symbol.id === row.symbol.id)!.api_count === 0);
  const facets = (key: string): Facet[] => {
    const counts = new Map<string, Facet>();
    for (const row of scoped.filter(r => matches(r, key))) {
      const values = key === "used_in" ? allRows.find(r => r.symbol.id === row.symbol.id)!.used_in.map(u => [u.key, `${query.repo ? "" : u.repo_id + " · "}${u.service_id}`])
        : key === "defined_in" ? [[row.owner.key, `${query.repo ? "" : row.symbol.repo_id + " · "}${row.owner.label}`]]
        : [[(row.symbol.props.kind || "function").toLowerCase(), (row.symbol.props.kind || "function").toLowerCase()]];
      for (const [key, label] of values) { const count = counts.get(key) || { key, label, count: 0 }; count.count++; counts.set(key, count); }
    }
    return [...counts.values()].sort((a, b) => a.label.localeCompare(b.label));
  };
  const rows = scoped.filter(r => matches(r));
  for (const row of rows) row.same_name_count = new Set(candidates.filter(r => r.symbol.name === row.symbol.name).map(r => r.owner.key)).size;
  rows.sort((a, b) => query.sort === "name" ? a.symbol.name.localeCompare(b.symbol.name) || a.symbol.id.localeCompare(b.symbol.id)
    : (query.sort === "callers" ? b.caller_count - a.caller_count : query.sort === "apis" ? b.api_count - a.api_count
      : b.service_count - a.service_count || b.api_count - a.api_count || b.caller_count - a.caller_count)
      || a.symbol.name.localeCompare(b.symbol.name) || a.symbol.id.localeCompare(b.symbol.id));
  return { items: rows.slice(query.offset || 0, (query.offset || 0) + (query.limit || 50)), total: rows.length,
    facets: { used_in: facets("used_in"), defined_in: facets("defined_in"), kind: facets("kind") } };
}
export function demoFunctionDetail(id: string): FunctionDetail {
  const symbol = byId.get(id);
  if (!symbol) throw new Error("Function not found");
  return { symbol, owner: owner(symbol), package: allRows.find(r => r.symbol.id === id)!.package, apis: pathsTo(symbol),
    callers: calls.filter(e => e.to_id === id).flatMap((edge: GEdge) => {
      const caller = byId.get(edge.from_id);
      return caller ? [{ symbol: caller, owner: owner(caller), confidence: "EXTRACTED", evidence: { file: caller.file, line: 5, rule: "call:demo" } }] : [];
    }) };
}
