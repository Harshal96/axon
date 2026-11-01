import { useEffect, useRef, useState } from "react";
import { EmptyState, FunctionInspector, FunctionTable, MultiSelect, Page, PageHeader, Segmented } from "@components";
import { facetSummary } from "@base/functions";
import type { Facet, FunctionDetail, FunctionsPage as FunctionResults, Route } from "@base/types";
import * as api from "@client";

type FunctionRoute = Extract<Route, { kind: "function" }>;
const empty: FunctionResults = { items: [], total: 0, facets: { used_in: [], defined_in: [], kind: [] } };

export function FunctionsPage({ route, multiRepo, go, openApi }: {
  route: FunctionRoute; multiRepo: boolean; go: (route: Route) => void; openApi: (id: string) => void;
}) {
  const [q, setQ] = useState(route.q || "");
  const [results, setResults] = useState(empty);
  const [detail, setDetail] = useState<FunctionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const goRef = useRef(go); goRef.current = go;
  const routeRef = useRef(route); routeRef.current = route;
  const used = route.used ?? [], defined = route.defined ?? [], kinds = route.kindFilter ?? [];
  const offset = route.offset || 0;
  const hideHandlers = route.hideHandlers !== false;
  const update = (patch: Partial<FunctionRoute>) => go({ ...route, offset: 0, ...patch });
  useEffect(() => { setQ(route.q || ""); }, [route.q]);
  useEffect(() => {
    if (q === (routeRef.current.q || "")) return;
    const timer = window.setTimeout(() => goRef.current({ ...routeRef.current, q: q || undefined, offset: 0 }), 250);
    return () => window.clearTimeout(timer);
  }, [q]);
  const queryKey = JSON.stringify({ q: (route.q || "").trim().length >= 2 ? route.q?.trim() : "",
    repo: route.repo, used_in: used, defined_in: defined, kind: kinds,
    hide_handlers: hideHandlers, unreached: !!route.unreached, sort: route.sort || "usage", limit: 50, offset });
  useEffect(() => {
    let live = true;
    setLoading(true); setError("");
    api.fetchFunctions(JSON.parse(queryKey)).then(data => {
      if (live) {
        setResults(data); setLoading(false);
        const currentOffset = routeRef.current.offset || 0;
        if (currentOffset && currentOffset >= data.total)
          goRef.current({ ...routeRef.current, offset: Math.max(0, Math.ceil(data.total / 50) * 50 - 50) });
      }
    }).catch(e => { if (live) { setError(String(e)); setLoading(false); } });
    return () => { live = false; };
  }, [queryKey]);
  useEffect(() => {
    let live = true;
    setDetail(null); setDetailError("");
    if (!route.fn) { setDetailLoading(false); return; }
    setDetailLoading(true);
    api.fetchFunctionDetail(route.fn).then(data => {
      if (live) { setDetail(data); setDetailLoading(false); }
    }).catch(e => { if (live) { setDetailError(String(e)); setDetailLoading(false); } });
    return () => { live = false; };
  }, [route.fn]);
  const active = used.length + defined.length + kinds.length;
  const closeInspector = () => {
    const selected = route.fn;
    go({ ...route, fn: undefined });
    window.requestAnimationFrame(() => {
      const row = [...document.querySelectorAll<HTMLElement>(".fn-table .trow")]
        .find(el => el.dataset.symbol === selected);
      row?.focus();
    });
  };
  const facetOptions = (options: Facet[], selected: string[]) => [
    ...options, ...selected.filter(key => !options.some(o => o.key === key)).map(key => ({ key, label: key, count: 0 })),
  ];
  const chips = (label: string, values: string[], facets: Facet[], key: "used" | "defined" | "kindFilter") =>
    values.map(value => <button key={`${key}-${value}`} className="chip cite" title={`Remove ${label}: ${value}`}
      onClick={() => update({ [key]: values.filter(v => v !== value) })}>
      {label}: {facetSummary([value], facets)} <span aria-hidden>×</span></button>);
  return <Page>
    <PageHeader title="Functions → APIs" sub={`${loading ? "Loading" : `${results.items.length} of ${results.total}`} · usage = static reach from indexed API handlers`} />
    <div className="filter-bar fn-filters">
      <input className="field filter-q wide" aria-label="Search functions" placeholder="Search function or method…"
        value={q} onChange={e => setQ(e.target.value)} />
      <MultiSelect label="Used in" options={facetOptions(results.facets.used_in, used)} value={used} onChange={value => update({ used: value })} />
      <MultiSelect label="Defined in" options={facetOptions(results.facets.defined_in, defined)} value={defined} onChange={value => update({ defined: value })} />
      <MultiSelect label="Kind" options={facetOptions(results.facets.kind, kinds)} value={kinds} onChange={value => update({ kindFilter: value })} />
      <Segmented options={["usage", "apis", "callers", "name"] as const} value={route.sort || "usage"}
        labels={{ usage: "Most used", apis: "APIs", callers: "Callers", name: "Name" }} ariaLabel="Sort functions"
        onChange={sort => update({ sort })} />
    </div>
    <div className="filter-bar fn-options">
      <label className="fn-toggle"><input type="checkbox" checked={hideHandlers} onChange={e => update({ hideHandlers: e.target.checked })} />Hide API handlers</label>
      <label className="fn-toggle"><input type="checkbox" checked={!!route.group} onChange={e => update({ group: e.target.checked })} />Group by name</label>
      <label className="fn-toggle"><input type="checkbox" checked={!!route.unreached} onChange={e => update({ unreached: e.target.checked })} />Not reached by any API</label>
      {active > 0 && <><span className="fn-filter-divider" />{chips("Used in", used, results.facets.used_in, "used")}
        {chips("Defined in", defined, results.facets.defined_in, "defined")}{chips("Kind", kinds, results.facets.kind, "kindFilter")}
        <button className="btn ghost sm" onClick={() => update({ used: [], defined: [], kindFilter: [] })}>Clear filters</button></>}
    </div>
    <div className="page-pad fn-content">
      {route.unreached && <div className="banner tone-info">Not reached by any indexed API. Static analysis may miss workers, CLIs, jobs, reflection and framework hooks. This is not evidence that code is unused.</div>}
      {error && <div className="banner tone-risk" role="alert">Could not load functions: {error}</div>}
      <div className="flow-shell fn-shell"><div className="flow-main" aria-busy={loading}>
        {loading ? <div className="fn-skeleton" role="status" aria-label="Loading functions">{Array.from({ length: 8 }, (_, i) => <div key={i} className="fn-skeleton-row" />)}</div>
          : !error && results.items.length ? <FunctionTable rows={results.items} selected={route.fn} grouped={!!route.group}
            unreached={!!route.unreached} multiRepo={multiRepo} onSelect={fn => go({ ...route, fn })} />
          : !error && <EmptyState title={q.trim().length >= 2 ? "No matching function" : active || route.unreached ? "No functions match these filters" : "No functions to show"}
            text={q.trim().length >= 2 ? "Try a different function or qualified name, or clear the filters." : active || route.unreached ? "Clear or adjust the filters to see indexed functions." : "Index a repository first, or turn off Hide API handlers to include its handlers."} />}
        {!loading && results.total > 50 && <nav className="row fn-pagination" aria-label="Function pages">
          <button className="btn sm" disabled={offset === 0} onClick={() => go({ ...route, offset: Math.max(0, offset - 50) })}>Previous</button>
          <span className="dim t-sm">{offset + 1}–{Math.min(offset + 50, results.total)} of {results.total}</span>
          <button className="btn sm" disabled={offset + 50 >= results.total} onClick={() => go({ ...route, offset: offset + 50 })}>Next</button></nav>}
      </div>
        {detailLoading && <aside className="inspector"><p role="status">Loading function evidence…</p><button className="btn sm" onClick={closeInspector}>Close inspector</button></aside>}
        {detailError && <aside className="inspector" role="alert"><p>{detailError}</p><button className="btn" onClick={closeInspector}>Close inspector</button></aside>}
        {detail && <FunctionInspector key={detail.symbol.id} detail={detail} multiRepo={multiRepo} used={used} repo={route.repo}
          onClose={closeInspector} openApi={openApi} go={go} />}
      </div>
    </div>
  </Page>;
}
