/** Browser-independent URL routing; repository identifiers are single encoded segments. */
import type { GNode, Route } from "./types.ts";

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
  if (view === "ask" || view === "function") {
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
