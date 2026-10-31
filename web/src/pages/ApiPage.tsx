/* API detail — one endpoint as an observability object: who calls
   it (left), its handler code, and what it calls (right), with the
   request flow animated along the edges. */
import { useEffect, useState } from "react";
import { ArrowRight, Bot } from "lucide-react";
import {
  Card, EgoGraph, EmptyState, MethodChip, Page, PageHeader, Section, Stat,
} from "@components";
import { nodeLabel } from "@base/catalog";
import type { ApiDetail, ApiGraph, Route } from "@base/types";
import * as api from "@client";

export function ApiPage({ repo, id, openApi, go }: {
  repo?: string; id: string; openApi: (id: string) => void; go: (r: Route) => void;
}) {
  const [graph, setGraph] = useState<ApiGraph | null>(null);
  const [detail, setDetail] = useState<ApiDetail | null>(null);
  useEffect(() => {
    let live = true;
    setGraph(null); setDetail(null);
    api.fetchGraph(id).then((g) => { if (live) setGraph(g); })
      .catch(() => undefined);
    api.fetchDetail(id).then((d) => { if (live) setDetail(d); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [id]);

  const ep = detail?.endpoint ?? graph?.endpoint;
  if (ep && repo && ep.repo_id !== repo) return <Page><EmptyState
    title="API not found in this repository" text="Check the repository and API in the link." /></Page>;
  const askQ = ep ? `Who calls ${ep.props.method} ${ep.props.path}?` : "";
  return (
    <Page>
      <PageHeader
        title={<span className="mono" style={{ fontSize: "var(--fs-lg)" }}>
          {ep ? nodeLabel(ep) : "API"}</span>}
        meta={ep && <>
          <MethodChip method={ep.props.method ?? ""} />
          {ep.props.deprecated === "true"
            && <span className="chip tone-off">deprecated in spec</span>}
          {detail && (detail.deprecatable
            ? <span className="chip tone-risk">no in-graph callers — deprecation candidate</span>
            : <span className="chip tone-ok">{detail.callers.length} caller(s)</span>)}
        </>}
        actions={ep && <>
          <button className="btn" onClick={() => go({ kind: "service", id: ep.service_id, repo: ep.repo_id })}>
            {ep.service_id}</button>
          <button className="btn primary" onClick={() => go({ kind: "ask", q: askQ })}>
            <Bot size={14} aria-hidden /> Ask about this API</button>
        </>} />
      <div className="page-pad stack" style={{ gap: 4 }}>
        {ep && (
          <div className="stats-grid">
            <Card><Stat label="Service" value={ep.service_id} sub={ep.repo_id} /></Card>
            <Card><Stat label="Handler"
              value={<span className="mono t-sm">{detail?.handler ? nodeLabel(detail.handler) : "—"}</span>}
              sub={ep.file ? `${ep.file}:${ep.line_start}` : "no handler in code"} /></Card>
            <Card><Stat label="In-graph callers" value={detail?.callers.length ?? "…"}
              sub="symbols calling this API" /></Card>
            <Card><Stat label="Outbound APIs" value={detail?.outbound.length ?? "…"}
              sub="reached from handler code" /></Card>
          </div>
        )}
        <Section title="Request flow" sub="callers → this API → handler code → calls">
          {graph ? <EgoGraph graph={graph} openApi={openApi} go={go} />
            : <EmptyState title="Loading graph…" text="Fetching the per-API ego graph." />}
        </Section>
        {detail && (
          <div className="split">
            <Section title="Callers" sub={`${detail.callers.length} in graph`}>
              <Card className="attn">
                {detail.callers.length === 0 && <p className="muted t-sm">
                  No indexed code calls this API. External or runtime-only
                  callers are not visible statically.</p>}
                {detail.callers.map((c) => (
                  <div key={c.id} className="attn-row static">
                    <span className="grow mono t-sm">{nodeLabel(c)}
                      <span className="dim"> · {c.service_id} · {c.file}:{c.line_start}</span>
                    </span>
                  </div>))}
              </Card>
            </Section>
            <Section title="Calls" sub={`${detail.outbound.length} outbound`}>
              <Card className="attn">
                {detail.outbound.length === 0 && <p className="muted t-sm">
                  This handler calls no other indexed API.</p>}
                {detail.outbound.map((o) => (
                  <button key={o.id} className="attn-row"
                    onClick={() => { if (o.kind === "ApiEndpoint") openApi(o.id); }}>
                    <ArrowRight size={13} aria-hidden />
                    <span className="grow mono t-sm">{nodeLabel(o)}
                      <span className="dim"> · {o.service_id || "external"}</span></span>
                    <span className="chip">{o.kind === "ExternalApi" ? "external" : o.service_id}</span>
                  </button>))}
              </Card>
            </Section>
          </div>
        )}
      </div>
    </Page>
  );
}
