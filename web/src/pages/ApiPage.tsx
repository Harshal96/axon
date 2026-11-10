/* API detail — one endpoint as an observability object: who calls
   it (left), its handler code, and what it calls (right), with the
   request flow animated along the edges. */
import { useEffect, useState } from "react";
import { ArrowRight, Bot, Wrench } from "lucide-react";
import {
  Card, EgoGraph, EmptyState, MethodChip, Page, PageHeader, Section, Stat,
} from "@components";
import { nodeLabel } from "@base/catalog";
import type { ApiDetail, ApiGraph, CorrectionBody, CorrectionPreview, Route } from "@base/types";
import * as api from "@client";

function FixApiPanel({ detail, onDone }: { detail: ApiDetail; onDone: () => void }) {
  const ep = detail.endpoint;
  const [handler, setHandler] = useState("");
  const [prompt, setPrompt] = useState("");
  const [toService, setToService] = useState("");
  const [toMethod, setToMethod] = useState("GET");
  const [toPath, setToPath] = useState("");
  const [evidenceFile, setEvidenceFile] = useState("");
  const [evidenceLine, setEvidenceLine] = useState("1");
  const [evidenceText, setEvidenceText] = useState("");
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
        to_service: toService.trim(), to_method: toMethod, to_path: toPath.trim(),
        evidence_file: evidenceFile.trim(), evidence_line: Number(evidenceLine) || 1,
        evidence_text: evidenceText.trim(),
      }];
    }
    if (suppressReason.trim()) {
      b.suppress = [{ to_service: suppressService.trim(), reason: suppressReason.trim() }];
    }
    if (prompt.trim()) b.prompt = prompt.trim();
    return b;
  };
  const hasOps = () => {
    const b = body();
    return Boolean(b.handler || b.add_calls?.length || b.suppress?.length);
  };

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
                  setToService(call.to_service); setToMethod(call.to_method); setToPath(call.to_path);
                  setEvidenceFile(call.evidence_file); setEvidenceLine(String(call.evidence_line));
                  setEvidenceText(call.evidence_text);
                }
                const sup = p.draft.suppress?.[0];
                if (sup) { setSuppressService(sup.to_service ?? ""); setSuppressReason(sup.reason); }
              }
              setMsg("Preview ready — check the axon.yaml diff before applying.");
            })}>
            Preview correction
          </button>
          <button className="btn primary" disabled={busy || !hasOps()}
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

export function ApiPage({ repo, id, openApi, go }: {
  repo?: string; id: string; openApi: (id: string) => void; go: (r: Route) => void;
}) {
  const [graph, setGraph] = useState<ApiGraph | null>(null);
  const [detail, setDetail] = useState<ApiDetail | null>(null);
  const [showFix, setShowFix] = useState(false);
  const [reloadTick, setReloadTick] = useState(0);
  useEffect(() => {
    let live = true;
    setGraph(null); setDetail(null);
    api.fetchGraph(id).then((g) => { if (live) setGraph(g); })
      .catch(() => undefined);
    api.fetchDetail(id).then((d) => { if (live) setDetail(d); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [id, reloadTick]);

  const implicit = detail?.handler_edges?.find((edge) => edge.props.implicit_action)?.props;
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
          <button className="btn" aria-expanded={showFix}
            onClick={() => setShowFix((v) => !v)}>
            <Wrench size={14} aria-hidden /> Fix this API</button>
        </>} />
      <div className="page-pad stack" style={{ gap: 4 }}>
        {ep && (
          <div className="stats-grid">
            <Card><Stat label="Service" value={ep.service_id} sub={ep.repo_id} /></Card>
            <Card><Stat label="Handler"
              value={<span className="mono t-sm">{detail?.handler ? nodeLabel(detail.handler) : "—"}</span>}
              sub={implicit ? `implicit ${implicit.implicit_action} from ${implicit.inherited_from}` : ep.file ? `${ep.file}:${ep.line_start}` : "no handler in code"} /></Card>
            <Card><Stat label="In-graph callers" value={detail?.callers.length ?? "…"}
              sub="symbols calling this API" /></Card>
            <Card><Stat label="Outbound APIs" value={detail?.outbound.length ?? "…"}
              sub="reached from handler code" /></Card>
          </div>
        )}
        {showFix && detail && (
          <Section title="Fix this API" sub="re-check or correct just this endpoint">
            <FixApiPanel detail={detail} onDone={() => setReloadTick((t) => t + 1)} />
          </Section>
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
