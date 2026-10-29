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
  return services
    .map((s) => {
      const mine = apis.filter(
        (a) => a.service_id === s.service_id && a.repo_id === s.repo_id);
      return {
        service: s,
        apis: s.api_count ?? mine.length,
        deprecated: mine.filter((a) => a.props.deprecated === "true").length,
      };
    })
    .sort((a, b) => a.service.service_id.localeCompare(b.service.service_id));
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
