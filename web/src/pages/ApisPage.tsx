/* APIs — the API catalog: every indexed endpoint, filterable by
   text, method, and service. Rows open the per-API graph. */
import { useEffect, useMemo, useState } from "react";
import { ApiTable, EmptyState, Page, PageHeader, PillTabs } from "@components";
import type { GNode } from "@base/types";
import * as api from "@client";

export function ApisPage({ service, services, openApi, goService }: {
  service: string; services: GNode[];
  openApi: (id: string) => void; goService: (id: string) => void;
}) {
  const [apis, setApis] = useState<GNode[]>([]);
  const [q, setQ] = useState("");
  const [method, setMethod] = useState("All methods");
  useEffect(() => {
    let live = true;
    api.fetchApis(service, q).then((a) => { if (live) setApis(a); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [service, q]);
  const methods = useMemo(() => ["All methods", ...[...new Set(
    apis.map((a) => (a.props.method || "").toUpperCase()).filter(Boolean))].sort()],
    [apis]);
  const shown = method === "All methods" ? apis
    : apis.filter((a) => (a.props.method || "").toUpperCase() === method);
  const deprecated = apis.filter((a) => a.props.deprecated === "true").length;
  return (
    <Page>
      <PageHeader title={service ? `APIs · ${service}` : "API catalog"}
        sub={`${shown.length} of ${apis.length} endpoints`}
        meta={deprecated > 0
          ? <span className="chip tone-off">{deprecated} deprecated</span> : undefined}
        actions={service
          ? <button className="btn" onClick={() => goService(service)}>
              Open {service}</button> : undefined} />
      <div className="filter-bar">
        <PillTabs options={methods} value={methods.includes(method) ? method : "All methods"}
          onChange={setMethod} ariaLabel="Method filter" />
        <span className="spacer" />
        <input className="field filter-q wide" placeholder="Filter by path, operation, or service…"
          aria-label="Filter APIs" value={q}
          onChange={(e) => setQ(e.target.value)} />
      </div>
      {!service && services.length > 1 && (
        <div className="filter-bar" style={{ paddingTop: 0 }}>
          <span className="dim t-sm">Service:</span>
          {services.map((s) => (
            <button key={s.id} className="pill" onClick={() => goService(s.service_id)}>
              {s.service_id} · {s.api_count ?? apis.filter((a) => a.service_id === s.service_id).length}
            </button>))}
        </div>)}
      <div className="page-pad" style={{ paddingTop: 8 }}>
        {shown.length === 0 ? (
          <EmptyState title="No APIs found"
            text="Index a repo first, or clear the filter." />
        ) : <ApiTable apis={shown} openApi={openApi} showService={!service} />}
      </div>
    </Page>
  );
}
