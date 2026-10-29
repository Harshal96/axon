/* Service detail — the service's APIs. Callers, outbound calls, and
   the request-flow graph live on each API's own page (per-API ego
   graph); this page never loads a whole-graph view. */
import { Bot } from "lucide-react";
import {
  ApiTable, Card, EmptyState, HealthBadge, MethodChip, Page, PageHeader,
  Section, Stat, serviceHealth,
} from "@components";
import { serviceRows } from "@base/catalog";
import type { GNode, Route } from "@base/types";

export function ServicePage({ id, services, apis, go, openApi }: {
  id: string; services: GNode[]; apis: GNode[];
  go: (r: Route) => void; openApi: (id: string) => void;
}) {
  const svc = services.find((s) => s.service_id === id);
  const svcApis = apis.filter((a) => a.service_id === id
    && (!svc || a.repo_id === svc.repo_id));
  if (!svc) {
    return <Page><div className="page-pad">
      <EmptyState title="Service not found"
        text={`No indexed service named ${id}. It may not be indexed yet.`} />
    </div></Page>;
  }
  const row = serviceRows([svc], svcApis)[0];
  const { h, why } = serviceHealth(row);
  const deprecated = svcApis.filter((a) => a.props.deprecated === "true").length;
  return (
    <Page>
      <PageHeader title={svc.service_id}
        icon={<span className="svc-ic lg">{svc.service_id[0]?.toUpperCase()}</span>}
        sub={svc.repo_id}
        meta={<><HealthBadge h={h} why={why} />
          <span className="chip">{svc.props.framework || svc.props.language || "auto"}</span></>}
        actions={<>
          <button className="btn" onClick={() => go({ kind: "apis", service: id })}>
            API catalog</button>
          <button className="btn primary"
            onClick={() => go({ kind: "ask", q: `What services does ${id} call?` })}>
            <Bot size={14} aria-hidden /> Ask about {id}</button>
        </>} />
      <div className="page-pad stack" style={{ gap: 4 }}>
        <div className="stats-grid">
          <Card><Stat label="APIs" value={svcApis.length}
            sub="in this service" /></Card>
          <Card><Stat label="Deprecated" value={deprecated}
            sub="marked in spec" /></Card>
          <Card><Stat label="Framework"
            value={svc.props.framework || svc.props.language || "auto"}
            sub={svc.repo_id} /></Card>
        </div>

        <Section title={`APIs · ${svcApis.length}`}>
          {svcApis.length === 0
            ? <EmptyState title="No APIs indexed" text="This service exposes no API in the graph yet." />
            : <ApiTable apis={svcApis} openApi={openApi} showService={false} />}
        </Section>
        <p className="dim t-sm">
          Tip: <MethodChip method="GET" /> rows open the per-API graph —
          callers on the left, handler code in the middle, outbound calls
          flowing right.
        </p>
      </div>
    </Page>
  );
}
