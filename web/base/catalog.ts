/* Pure catalog derivations for the catalog views.
 *
 * Every number here comes from the service and API lists alone.
 * Cross-service call structure is intentionally not aggregated here:
 * it is explored one API at a time on the per-API graph, never as a
 * whole-graph overview (that does not scale to monorepos).
 *
 * Relative imports only + type-only references (node --test loads it).
 */
import type { GNode } from "./types.ts";

export function nodeLabel(n: GNode): string {
  return n.label ?? n.props.qualified ?? n.name;
}

export interface ServiceRow {
  service: GNode;
  apis: number;
  deprecated: number;
}

export function serviceRows(services: GNode[], apis: GNode[]): ServiceRow[] {
  const groups = new Map<string, { apis: number; deprecated: number }>();
  for (const api of apis) {
    const key = JSON.stringify([api.repo_id, api.service_id]);
    const counts = groups.get(key) ?? { apis: 0, deprecated: 0 };
    counts.apis++; if (api.props.deprecated === "true") counts.deprecated++;
    groups.set(key, counts);
  }
  return services.map(service => {
    const counts = groups.get(JSON.stringify([service.repo_id, service.service_id]));
    return { service, apis: service.api_count ?? counts?.apis ?? 0,
      deprecated: counts?.deprecated ?? 0 };
  }).sort((a, b) => a.service.service_id.localeCompare(b.service.service_id));
}

/** HTTP method -> meaning tone class (chip). Unknown methods stay neutral. */
export function methodTone(method: string): string {
  switch ((method || "").toUpperCase()) {
    case "GET": return "tone-info";
    case "POST": return "tone-ok";
    case "PUT":
    case "PATCH": return "tone-risk";
    case "DELETE": return "tone-off";
    default: return "";
  }
}

/** Partition one sorted catalog so overflow cannot omit or repeat services. */
export function serviceFilterGroups(services: GNode[], apis: GNode[]) {
  const rows = serviceRows(services, apis);
  return { pills: rows.slice(0, 20), overflow: rows.slice(20) };
}
