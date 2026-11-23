/* API detail — one endpoint as an observability object: who calls
   it (left), its handler code, and what it calls (right), with the
   request flow animated along the edges. */
import { useEffect, useState } from "react";
import { ArrowRight, ArrowUpRight, Bot, Wrench } from "lucide-react";
import {
  Card, EgoGraph, EmptyState, MethodChip, Page, PageHeader, Section, Stat,
} from "@components";
import { nodeLabel } from "@base/catalog";
import { definitionUrl } from "@base/functions";
import { functionRoute } from "@base/routing";
import { evidenceLabel } from "@base/utils";
import type { ApiDetail, ApiView, CallSite, CorrectionBody, CorrectionPreview, Route } from "@base/types";
import * as api from "@client";

function FixApiPanel({ detail, onDone }: { detail: ApiDetail; onDone: () => void }) {
  const ep = detail.endpoint;
  const [handler, setHandler] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toRepo, setToRepo] = useState("");
  const [toService, setToService] = useState("");
  const [toMethod, setToMethod] = useState("GET");
  const [toPath, setToPath] = useState("");
  const [evidenceFile, setEvidenceFile] = useState("");
  const [evidenceLine, setEvidenceLine] = useState("1");
  const [evidenceText, setEvidenceText] = useState("");
  const [suppressRepo, setSuppressRepo] = useState("");
  const [suppressService, setSuppressService] = useState("");
  const [suppressReason, setSuppressReason] = useState("");
  const [preview, setPreview] = useState<CorrectionPreview | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const body = (): CorrectionBody => {
    const b: CorrectionBody = {};
    if (handler.trim()) b.handler = handler.trim();
    if (toService.trim() && toPath.trim()) {
      b.add_calls = [{
        to_repo: toRepo.trim(), to_service: toService.trim(), to_method: toMethod, to_path: toPath.trim(),
        evidence_file: evidenceFile.trim(), evidence_line: Number(evidenceLine) || 1,
        evidence_text: evidenceText.trim(),
      }];
    }
    if (suppressReason.trim()) {
      b.suppress = [{ to_repo: suppressRepo.trim(), to_service: suppressService.trim(), reason: suppressReason.trim() }];
    }
    if (prompt.trim()) b.prompt = prompt.trim();
    return b;
  };
  const hasOps = () => {
    const b = body();
    return Boolean(b.handler || b.add_calls?.length || b.suppress?.length);
  };
  const writable = preview?.correction_writable !== false;

  const pollJob = async (jobId: string): Promise<void> => {
    for (let i = 0; i < 120; i++) {
      const job = await api.fetchIndexJob(jobId);
      if (job.status === "done") {
        const changed = (job.result as { changed?: boolean } | null)?.changed;
        setMsg(changed === false
          ? "Re-check finished: no graph change for this API. The code may hit a parser blind spot — add a correction below."
          : "Re-check finished: graph updated for this API.");
        onDone();
        return;
      }
      if (job.status === "error" || job.status === "cancelled" || job.status === "interrupted") {
        setMsg(`Re-check ${job.status}: ${job.error ?? ""}`);
        return;
      }
      setMsg(`Re-checking… ${job.phase ?? job.status}`);
      await new Promise((r) => setTimeout(r, 1500));
    }
    setMsg("Re-check still running; refresh later to see the result.");
  };

  const run = async (fn: () => Promise<void>): Promise<void> => {
    setBusy(true);
    try { await fn(); } catch (e) { setMsg(String(e)); } finally { setBusy(false); }
  };

  return (
    <Card className="attn">
      <div className="stack">
        <p className="muted t-sm">
          Re-check fresh-parses only this API&apos;s handler files. A correction is written to
          axon.yaml as a manual handler/call/suppress entry, so it survives a full re-index.
          {detail.correction_llm_configured
            ? " A prompt can draft a correction for review; it is never applied directly."
            : " Prompt drafting is off (no LLM configured on the server); structured fields need no model."}
        </p>
        <div className="row">
          <button className="btn primary" disabled={busy}
            onClick={() => void run(async () => {
              setMsg("Starting re-check…");
              const r = await api.recheckApi(ep.id);
              await pollJob(r.job_id);
            })}>
            Re-check this API
          </button>
        </div>
        <label className="lab" htmlFor="fix-handler">Correct handler (qualified symbol)</label>
        <input id="fix-handler" className="field" placeholder="module.Class.method"
          value={handler} onChange={(e) => setHandler(e.target.value)} />
        <label className="lab" htmlFor="fix-prompt">Or describe what it should be (prompt draft)</label>
        <textarea id="fix-prompt" className="field" rows={2}
          placeholder="e.g. handled by orders.views.create_order and calls POST /score on fraud"
          value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        <label className="lab" htmlFor="fix-call">Add outbound call</label>
        <div className="row">
          <input className="field" aria-label="Target repository" placeholder="target repo (blank = local)"
            value={toRepo} onChange={(e) => setToRepo(e.target.value)} />
          <input id="fix-call" className="field" aria-label="Target service" placeholder="target service"
            value={toService} onChange={(e) => setToService(e.target.value)} />
          <input className="field" aria-label="Target method" placeholder="GET"
            value={toMethod} onChange={(e) => setToMethod(e.target.value)} />
          <input className="field grow" aria-label="Target path" placeholder="/path"
            value={toPath} onChange={(e) => setToPath(e.target.value)} />
        </div>
        <div className="row">
          <input className="field grow" aria-label="Evidence file" placeholder="evidence file (repo-relative)"
            value={evidenceFile} onChange={(e) => setEvidenceFile(e.target.value)} />
          <input className="field" aria-label="Evidence line" type="number" min={1}
            value={evidenceLine} onChange={(e) => setEvidenceLine(e.target.value)} />
          <input className="field grow" aria-label="Evidence text" placeholder="evidence source text"
            value={evidenceText} onChange={(e) => setEvidenceText(e.target.value)} />
        </div>
        <label className="lab" htmlFor="fix-suppress">Suppress a wrong outbound edge</label>
        <div className="row">
          <input className="field" aria-label="Suppress target repository" placeholder="target repo (blank = local)"
            value={suppressRepo} onChange={(e) => setSuppressRepo(e.target.value)} />
          <input id="fix-suppress" className="field" aria-label="Suppress target service" placeholder="target service (optional)"
            value={suppressService} onChange={(e) => setSuppressService(e.target.value)} />
          <input className="field grow" aria-label="Suppress reason" placeholder="reason (required to suppress)"
            value={suppressReason} onChange={(e) => setSuppressReason(e.target.value)} />
        </div>
        <div className="row">
          <button className="btn" disabled={busy || (!hasOps() && !prompt.trim())}
            onClick={() => void run(async () => {
              const p = await api.previewCorrection(ep.id, body());
              setPreview(p);
              if (p.draft) {
                if (p.draft.handler) setHandler(p.draft.handler);
                const call = p.draft.add_calls?.[0];
                if (call) {
                  setToRepo(call.to_repo ?? ""); setToService(call.to_service); setToMethod(call.to_method); setToPath(call.to_path);
                  setEvidenceFile(call.evidence_file); setEvidenceLine(String(call.evidence_line));
                  setEvidenceText(call.evidence_text);
                }
                const sup = p.draft.suppress?.[0];
                if (sup) { setSuppressRepo(sup.to_repo ?? ""); setSuppressService(sup.to_service ?? ""); setSuppressReason(sup.reason); }
              }
              setMsg("Preview ready — check the axon.yaml diff before applying.");
            })}>
            Preview correction
          </button>
          <button className="btn primary" disabled={busy || !hasOps() || !writable}
            onClick={() => void run(async () => {
              const r = await api.applyCorrection(ep.id, body());
              setPreview(r);
              setMsg("Correction written to axon.yaml. Re-checking this API…");
              if (r.job_id) await pollJob(r.job_id);
            })}>
            Apply + re-check
          </button>
        </div>
        {preview && (
          <div className="stack">
            {!writable && <p className="f-hint">The checkout is read-only. Apply the previewed changes in a writable checkout and re-index it.</p>}
            {preview.warnings.map((w) => <p key={w} className="f-hint">{w}</p>)}
            {Object.entries(preview.diffs).map(([file, diff]) => (
              <div key={file}>
                <p className="mono t-sm">{file}</p>
                <pre className="mono t-sm" style={{ whiteSpace: "pre-wrap" }}>{diff}</pre>
              </div>
            ))}
            {preview.effects.length > 0 && <p className="muted t-sm">
              Predicted effects: {preview.effects.map((e) => String(e.kind)).join(", ")} (MANUAL confidence, applied on re-check).</p>}
          </div>
        )}
        {msg && <p className="f-hint" role="status">{msg}</p>}
      </div>
    </Card>
  );
}

function CallSiteEvidence({ sites, openApi, unresolved = false }: {
  sites: CallSite[]; openApi: (id: string) => void; unresolved?: boolean;
}) {
  return <details className="call-sites">
    <summary>{unresolved ? "Why this call is external" : `${sites.length} call site${sites.length === 1 ? "" : "s"}`}</summary>
    <ul className="stack">
      {sites.map((site, i) => {
        const target = [site.target_repo, site.target_service].filter(Boolean).join("::");
        const evidence = site.evidence;
        return <li key={i}>
          {evidence?.file && <span className="mono">{evidence.file}:{evidence.line}</span>}
          <span className="dim"> · {evidenceLabel(evidence?.rule, site.props)}{site.confidence ? ` · ${site.confidence}` : ""}</span>
          {site.reason && <div>{site.reason.replaceAll("-", " ")}{target ? ` · ${target}` : ""}</div>}
          {site.candidate_nodes?.map(candidate => <button key={candidate.id} type="button" className="btn sm"
            onClick={() => openApi(candidate.id)}>{candidate.repo_id}::{candidate.service_id} · {nodeLabel(candidate)}</button>)}
        </li>;
      })}
    </ul>
  </details>;
}

function sitesBy(sites: CallSite[] | undefined, key: "from_id" | "to_id") {
  const groups = new Map<string, CallSite[]>();
  for (const site of sites ?? []) {
    const group = groups.get(site[key]);
    if (group) group.push(site);
    else groups.set(site[key], [site]);
  }
  return groups;
}

export function ApiPage({ repo, id, refresh, openApi, go }: {
  repo?: string; id: string; refresh: number; openApi: (id: string) => void; go: (r: Route) => void;
}) {
  const [view, setView] = useState<ApiView | null>(null);
  const [error, setError] = useState<{ message: string; missing: boolean } | null>(null);
  const [showFix, setShowFix] = useState(false);
  const [reloadTick, setReloadTick] = useState(0);
  const retry = () => setReloadTick(t => t + 1);
  useEffect(() => {
    let live = true;
    setError(null);
    api.fetchView(id).then(result => { if (live) setView(result); }).catch((e: unknown) => {
      if (live) setError({
        message: e instanceof Error ? e.message : String(e),
        missing: e instanceof Error && "status" in e && e.status === 404,
      });
    });
    return () => { live = false; };
  }, [id, refresh, reloadTick]);

  if (error) return <Page><EmptyState
    title={error.missing ? "API no longer indexed" : "Could not load this API"}
    text={error.missing ? "Check the repository's API catalog or re-index the checkout." : error.message}
    action={<button className="btn" onClick={retry}>Retry</button>} /></Page>;
  if (!view || view.detail.endpoint.id !== id) return <Page><EmptyState
    title="Loading graph…" text="Fetching the request flow." /></Page>;
  const { graph, detail } = view;
  const implicit = detail.handler_edges?.find((edge) => edge.props.implicit_action)?.props;
  const ep = detail.endpoint;
  if (repo && ep.repo_id !== repo) return <Page><EmptyState
    title="API not found in this repository" text="Check the repository and API in the link." /></Page>;
  const askQ = `Who calls ${JSON.stringify(ep.id)}?`;
  const handler = detail.handler;
  const handlerCount = detail.handlers?.length ?? 0;
  const definition = handler && definitionUrl(detail.handler_source_path, handler.file, handler.line_start);
  const callersById = sitesBy(detail.caller_sites, "from_id");
  const targetsById = sitesBy(detail.outbound_sites, "to_id");
  return (
    <Page>
      <PageHeader
        title={<span className="mono" style={{ fontSize: "var(--fs-lg)" }}>
          {nodeLabel(ep)}</span>}
        meta={<>
          <MethodChip method={ep.props.method ?? ""} />
          {ep.props.deprecated === "true"
            && <span className="chip tone-off">deprecated in spec</span>}
          {detail.deprecatable
            ? <span className="chip tone-risk">no in-graph callers — deprecation candidate</span>
            : <span className="chip tone-ok">{detail.callers.length} caller(s)</span>}
        </>}
        actions={<>
          <button className="btn" onClick={() => go({ kind: "service", id: ep.service_id, repo: ep.repo_id })}>
            {ep.service_id}</button>
          <button className="btn primary" onClick={() => go({ kind: "ask", q: askQ, repo: ep.repo_id })}>
            <Bot size={14} aria-hidden /> Ask about this API</button>
          <button className="btn" aria-expanded={showFix}
            onClick={() => setShowFix((v) => !v)}>
            <Wrench size={14} aria-hidden /> Fix this API</button>
        </>} />
      <div className="page-pad stack" style={{ gap: 4 }}>
        <div className="stats-grid">
          <Card><Stat label="Service" value={ep.service_id} sub={ep.repo_id} /></Card>
          <Card><Stat label="Handler"
            value={<span className="mono t-sm">{handler ? nodeLabel(handler)
              : handlerCount > 1 ? `${handlerCount} handlers` : "—"}</span>}
            sub={implicit ? `implicit ${implicit.implicit_action} from ${implicit.inherited_from}`
              : handler?.file ? `${handler.file}:${handler.line_start}`
              : handlerCount > 1 ? "See request flow" : "No handler in code"} />
            {definition && <a className="btn sm" href={definition}>Open definition</a>}</Card>
          <Card><Stat label="In-graph callers" value={detail.callers.length}
            sub="symbols calling this API" /></Card>
          <Card><Stat label="Outbound APIs" value={detail.outbound.length}
            sub="reached from handler code" /></Card>
        </div>
        {showFix && (
          <Section title="Fix this API" sub="re-check or correct just this endpoint">
            <FixApiPanel detail={detail} onDone={retry} />
          </Section>
        )}
        <Section title="Request flow" sub="callers → this API → handler code → calls">
          <EgoGraph graph={graph} openApi={openApi} go={go} />
        </Section>
        <div className="split">
          <Section title="Callers" sub={`${detail.callers.length} in graph`}>
            <Card className="attn">
              {detail.callers.length === 0 && <p className="muted t-sm">
                No indexed code calls this API. External or runtime-only
                callers are not visible statically.</p>}
              {detail.callers.map((c) => {
                const sites = callersById.get(c.id) ?? [];
                return <div key={c.id}>
                  <button className="attn-row" onClick={() => go(functionRoute(c))}>
                    <ArrowUpRight size={13} aria-hidden />
                    <span className="grow mono t-sm">{nodeLabel(c)}
                      <span className="dim"> · {c.repo_id}::{c.service_id} · {c.file}:{c.line_start}</span>
                    </span>
                  </button>
                  {sites.length > 0 && <CallSiteEvidence sites={sites} openApi={openApi} />}
                </div>;
              })}
            </Card>
          </Section>
          <Section title="Calls" sub={`${detail.outbound.length} outbound`}>
            <Card className="attn">
              {detail.outbound.length === 0 && <p className="muted t-sm">
                This handler calls no other indexed API.</p>}
              {detail.outbound.map((o) => {
                const sites = targetsById.get(o.id) ?? [];
                const external = o.kind !== "ApiEndpoint";
                return <div key={o.id}>
                  {external ? <div className="attn-row static">
                    <span className="grow mono t-sm">{nodeLabel(o)}</span><span className="chip">external</span>
                  </div> : <button className="attn-row" onClick={() => openApi(o.id)}>
                    <ArrowRight size={13} aria-hidden />
                    <span className="grow mono t-sm">{nodeLabel(o)}
                      <span className="dim"> · {o.repo_id}::{o.service_id}</span>
                    </span>
                  </button>}
                  {sites.length > 0 && <CallSiteEvidence sites={sites} openApi={openApi} unresolved={external} />}
                </div>;
              })}
            </Card>
          </Section>
        </div>
      </div>
    </Page>
  );
}
