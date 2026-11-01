/** Browser-independent URL routing; repository identifiers are single encoded segments. */
import type { FunctionSort, GNode, Route } from "./types.ts";

export function parseRoute(location: Pick<URL, "pathname" | "search">): Route {
  const match = /^\/repos\/([^/]+)\/?$/.exec(location.pathname);
  let repo = "";
  try { repo = match ? decodeURIComponent(match[1]) : ""; } catch { /* malformed link */ }
  const scope = repo ? { repo } : {};
  const params = new URLSearchParams(location.search);
  const service = params.get("service") || "";
  const id = params.get("api") || "";
  if (id) return { kind: "api", ...scope, id, ...(service ? { service } : {}) };
  const view = params.get("view");
  if (view === "function") {
    const sort = params.get("sort");
    const used = params.getAll("used"), defined = params.getAll("defined"), kinds = params.getAll("kind");
    const rawOffset = Number(params.get("offset"));
    const offset = Number.isFinite(rawOffset) ? Math.max(0, Math.floor(rawOffset)) : 0;
    return { kind: "function", ...scope, ...(params.get("q") ? { q: params.get("q")! } : {}),
      ...(used.length ? { used } : {}), ...(defined.length ? { defined } : {}), ...(kinds.length ? { kindFilter: kinds } : {}),
      ...(["usage", "apis", "callers", "name"].includes(sort || "") ? { sort: sort as FunctionSort } : {}),
      ...(params.get("fn") ? { fn: params.get("fn")! } : {}),
      ...(params.get("hide_handlers") === "false" ? { hideHandlers: false } : {}),
      ...(params.get("unreached") === "true" ? { unreached: true } : {}),
      ...(params.get("group") === "true" ? { group: true } : {}), ...(offset ? { offset } : {}) };
  }
  if (view === "ask") {
    const q = params.get("q");
    return { kind: view, ...scope, ...(q ? { q } : {}) };
  }
  if (view === "services" || view === "impact" || view === "onboard")
    return { kind: view, ...scope };
  if (service && view !== "apis") return { kind: "service", ...scope, id: service };
  return { kind: "apis", ...scope, ...(service ? { service } : {}) };
}

export function routeUrl(route: Route): string {
  const path = route.repo ? `/repos/${encodeURIComponent(route.repo)}` : "/";
  const params = new URLSearchParams();
  if (route.kind === "service") params.set("service", route.id);
  else if (route.kind === "api") {
    if (route.service) params.set("service", route.service);
    params.set("api", route.id);
  } else if (route.kind === "apis") {
    if (route.service) { params.set("view", "apis"); params.set("service", route.service); }
  } else {
    params.set("view", route.kind);
    if ((route.kind === "ask" || route.kind === "function") && route.q) params.set("q", route.q);
  }
  if (route.kind === "function") {
    for (const v of route.used ?? []) params.append("used", v);
    for (const v of route.defined ?? []) params.append("defined", v);
    for (const v of route.kindFilter ?? []) params.append("kind", v);
    if (route.sort && route.sort !== "usage") params.set("sort", route.sort);
    if (route.fn) params.set("fn", route.fn);
    if (route.hideHandlers === false) params.set("hide_handlers", "false");
    if (route.unreached) params.set("unreached", "true");
    if (route.group) params.set("group", "true");
    if (route.offset) params.set("offset", String(route.offset));
  }
  return path + (params.size ? `?${params}` : "");
}

/** Keep scope across navigation, but follow explicit endpoint/service ownership. */
export function resolveRoute(next: Route, current: Route,
  services: Pick<GNode, "repo_id" | "service_id">[],
  apis: Pick<GNode, "id" | "repo_id" | "service_id">[]): Route {
  let repo = next.repo ?? current.repo;
  if (next.kind === "api") {
    const endpoint = apis.find(a => a.id === next.id);
    // Graph node IDs remain usable even before the catalog has loaded.
    const parts = next.id.startsWith("endpoint::") ? next.id.split("::") : [];
    repo = endpoint?.repo_id ?? (parts.length >= 4 ? parts[1] : repo);
    const service = endpoint?.service_id ?? (parts.length >= 4 ? parts[2] : next.service);
    return { ...next, ...(repo ? { repo } : {}), ...(service ? { service } : {}) };
  }
  if (next.kind === "service" && next.repo === undefined && !repo) {
    const matches = services.filter(s => s.service_id === next.id);
    if (matches.length === 1) repo = matches[0].repo_id;
  }
  return { ...next, ...(repo ? { repo } : {}) };
}


/** Keep function exploration open when the repository switcher changes scope. */
export function repositoryRoute(current: Route, repo: string): Route {
  if (current.kind === "function") return { ...current, repo, used: undefined,
    defined: undefined, fn: undefined, offset: 0 };
  return repo ? { kind: "services", repo } : { kind: "apis", repo: "" };
}
