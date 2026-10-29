/* Catalog pieces — shared by Services, APIs, and the service/API
   detail pages. Health here is graph-derived (deprecated or missing
   APIs), never runtime telemetry. */
import type { GNode } from "../../base/types";
import { methodTone, nodeLabel, type ServiceRow } from "../../base/catalog";

export function MethodChip({ method }: { method: string }) {
  return <span className={`chip method ${methodTone(method)}`}>{method || "—"}</span>;
}

export function SourceChip({ api }: { api: GNode }) {
  const src = api.props.source ?? "both";
  if (src === "spec") return <span className="chip tone-risk" title="In the spec, no handler seen in code">spec only</span>;
  if (src === "code") return <span className="chip tone-info" title="In code, no spec entry">code only</span>;
  return <span className="chip" title="Seen in both code and spec">code + spec</span>;
}

export type ServiceHealth = "ok" | "risk" | "off";

export function serviceHealth(row: ServiceRow): { h: ServiceHealth; why: string } {
  if (row.apis === 0) return { h: "off", why: "No APIs indexed for this service" };
  if (row.deprecated > 0)
    return { h: "risk", why: `${row.deprecated} deprecated API${row.deprecated === 1 ? "" : "s"}` };
  return { h: "ok", why: "APIs indexed, none deprecated" };
}

export function HealthBadge({ h, why }: { h: ServiceHealth; why?: string }) {
  const glyph = h === "ok" ? "↗" : h === "risk" ? "↝" : "↘";
  const label = h === "ok" ? "On track" : h === "risk" ? "At risk" : "Empty";
  return (
    <span className={`health h-${h}`} title={why}>
      <span className="hg">{glyph}</span>{label}
    </span>
  );
}

export function ApiTable({ apis, openApi, showService = true }: {
  apis: GNode[];
  openApi: (id: string) => void;
  showService?: boolean;
}) {
  return (
    <table className="apis">
      <thead>
        <tr>
          <th>Endpoint</th>
          {showService && <th>Service</th>}
          <th>Source</th><th>Handler</th><th>Location</th>
        </tr>
      </thead>
      <tbody>
        {apis.map((a) => (
          <tr key={a.id} className="trow" onClick={() => openApi(a.id)} tabIndex={0}
            aria-label={`Open ${nodeLabel(a)}`}
            onKeyDown={(e) => { if (e.key === "Enter") openApi(a.id); }}>
            <td>
              <span className="row wrap" style={{ gap: 7 }}>
                <MethodChip method={a.props.method ?? ""} />
                <span className="mono">{a.props.path}</span>
                {a.props.deprecated === "true"
                  && <span className="chip tone-off">deprecated</span>}
              </span>
            </td>
            {showService && <td>{a.service_id}</td>}
            <td><SourceChip api={a} /></td>
            <td className="mono dim">{a.props.operation_id ?? "—"}</td>
            <td className="mono dim">{a.file ? `${a.file}:${a.line_start}` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
