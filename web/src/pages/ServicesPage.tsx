/* Services — the service catalog. Light rows derived from the
   service/API lists only; call structure is explored per API, never
   as a whole-graph overview. */
import { useMemo, useState } from "react";
import {
  EmptyState, Page, PageHeader,
} from "@components";
import { serviceRows } from "@base/catalog";
import type { GNode, Route } from "@base/types";
import { IndexCard } from "./IndexCard";

export function ServicesPage({ services, apis, go, bump }: {
  services: GNode[]; apis: GNode[];
  go: (r: Route) => void; bump: () => void;
}) {
  const [q, setQ] = useState("");
  const rows = useMemo(() => serviceRows(services, apis), [services, apis]);
  const shown = rows.filter((r) =>
    r.service.service_id.toLowerCase().includes(q.toLowerCase()));
  return (
    <Page>
      <PageHeader title="Services"
        sub={`${services.length} indexed`}
        actions={<button className="btn primary" onClick={() => go({ kind: "onboard" })}>
          + Onboard repo</button>} />
      <div className="filter-bar">
        <span className="spacer" />
        <input className="field filter-q" placeholder="Filter services…"
          aria-label="Filter services" value={q}
          onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="page-pad stack" style={{ paddingTop: 8 }}>
        <IndexCard bump={bump} />
        {services.length === 0 ? (
          <EmptyState title="No services indexed yet"
            text="Index a repo above, or run `axon index --repo .` from the CLI." />
        ) : (
          <div className="svc-table svc-table-light" role="table" aria-label="Service catalog">
            <div className="svc-head" role="row">
              <span>Service</span><span>APIs</span>
              <span>Deprecated</span>
            </div>
            {shown.map((r) => (
                <button key={r.service.id} className="svc-row trow" role="row"
                  onClick={() => go({ kind: "service", id: r.service.service_id, repo: r.service.repo_id })}>
                  <span className="svc-name">
                    <span className="svc-ic">{r.service.service_id[0]?.toUpperCase()}</span>
                    <span className="grow truncate">
                      <span className="svc-title">{r.service.service_id}</span>
                      <span className="dim t-sm">
                        {r.service.repo_id} · {r.service.props.framework || r.service.props.language || "auto"}
                      </span>
                    </span>
                  </span>
                  <span className="muted">{r.apis}</span>
                  <span className="muted">{r.deprecated}</span>
                </button>))}
            {shown.length === 0 && (
              <p className="dim t-sm" style={{ padding: "12px 4px" }}>
                No services match — clear the filter.</p>)}
          </div>
        )}
      </div>
    </Page>
  );
}
