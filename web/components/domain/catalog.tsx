/* Catalog pieces shared by the service and API pages. */
import { useState } from "react";
import type { GNode } from "@base/types";
import { methodTone, nodeLabel } from "@base/catalog";

export function MethodChip({ method }: { method: string }) {
  return <span className={`chip method ${methodTone(method)}`}>{method || "—"}</span>;
}

export function SourceChip({ api }: { api: GNode }) {
  const src = api.props.source ?? "both";
  if (src === "spec") return <span className="chip tone-risk" title="In the spec, no handler seen in code">spec only</span>;
  if (src === "code") return <span className="chip tone-info" title="In code, no spec entry">code only</span>;
  return <span className="chip" title="Seen in both code and spec">code + spec</span>;
}

export function ApiTable({ apis, openApi, showService = true }: {
  apis: GNode[];
  openApi: (id: string) => void;
  showService?: boolean;
}) {
  const [page, setPage] = useState(0);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(apis.length / 100) - 1));
  return (
    <>
    <table className="apis">
      <thead>
        <tr>
          <th>Endpoint</th>
          {showService && <th>Service</th>}
          <th>Source</th><th>Handler</th><th>Location</th>
        </tr>
      </thead>
      <tbody>
        {apis.slice(currentPage * 100, (currentPage + 1) * 100).map((a) => (
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
    {apis.length > 100 && <div className="row" role="navigation" aria-label="API pages">
      <button className="btn sm" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
      <span className="dim t-sm">{currentPage * 100 + 1}–{Math.min((currentPage + 1) * 100, apis.length)} of {apis.length}</span>
      <button className="btn sm" disabled={(currentPage + 1) * 100 >= apis.length} onClick={() => setPage(currentPage + 1)}>Next</button>
    </div>}
    </>
  );
}
