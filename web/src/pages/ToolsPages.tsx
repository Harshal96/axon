/* Explore tools — Function → APIs, PR impact, Onboard repo.
   Same behaviour as before the redesign, on Arc page scaffolding. */
import { useEffect, useState } from "react";
import { Card, EmptyState, Page, PageHeader, Section } from "@components";
import { nodeLabel } from "@base/catalog";
import type { GNode, ImpactResult, OnboardReport } from "@base/types";
import * as api from "@client";

export function FunctionPage({ initial, openApi }: {
  initial: string; openApi: (id: string) => void;
}) {
  const [q, setQ] = useState(initial);
  const [rows, setRows] = useState<{ symbol: GNode; apis: GNode[] }[]>([]);
  const [searched, setSearched] = useState(false);
  const run = async (term = q): Promise<void> => {
    if (term.trim().length < 2) return;
    setSearched(true);
    setRows(await api.fetchFunctionApis(term.trim()));
  };
  useEffect(() => { if (initial.trim().length >= 2) void run(initial); /* once */ }, []); // eslint-disable-line
  return (
    <Page>
      <PageHeader title="Function → APIs"
        sub="which APIs include a function in their handler closure" />
      <div className="page-pad stack">
        <div className="row">
          <input className="field grow" placeholder="Function or method name — try price_cart"
            aria-label="Function name" value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void run(); }} />
          <button className="btn primary" onClick={() => void run()}>Find APIs</button>
        </div>
        {searched && rows.length === 0 && (
          <EmptyState title="No matching symbol"
            text="No indexed function matches that name." />)}
        {rows.map(({ symbol, apis: found }) => (
          <Card key={symbol.id}>
            <div className="row between" style={{ marginBottom: 8 }}>
              <span className="mono">{nodeLabel(symbol)}</span>
              <span className="dim mono t-sm">{symbol.file}:{symbol.line_start}</span>
            </div>
            <div className="stack">
              {found.length === 0 && <span className="muted t-sm">No indexed API includes it.</span>}
              {found.map((a) => (
                <button key={a.id} className="pill" style={{ alignSelf: "flex-start" }}
                  onClick={() => openApi(a.id)}>
                  {nodeLabel(a)} <span className="dim">{a.service_id}</span>
                </button>))}
            </div>
          </Card>
        ))}
      </div>
    </Page>
  );
}

export function ImpactPage() {
  const [repoPath, setRepoPath] = useState("");
  const [files, setFiles] = useState("");
  const [result, setResult] = useState<ImpactResult | null>(null);
  const [err, setErr] = useState("");
  const run = async (): Promise<void> => {
    setErr("");
    try {
      setResult(await api.impact(
        repoPath, files.split("\n").map((s) => s.trim()).filter(Boolean)));
    } catch (e) { setErr(String(e)); }
  };
  return (
    <Page>
      <PageHeader title="PR impact" sub="changed files → affected APIs and services" />
      <div className="page-pad stack">
        {__AXON_DEMO__ && (
          <div className="banner tone-risk">
            Demo build: impact needs the live API. In CI, the GitHub Action runs this for you.
          </div>)}
        <Card>
          <label className="lab" htmlFor="imp-repo">Repo path on the server</label>
          <input id="imp-repo" className="field" value={repoPath}
            onChange={(e) => setRepoPath(e.target.value)} placeholder="/path/to/repo" />
          <label className="lab" htmlFor="imp-files">Changed files (one per line)</label>
          <textarea id="imp-files" className="field" rows={5} value={files}
            onChange={(e) => setFiles(e.target.value)}
            placeholder={"services/payment/app.py"} />
          <button className="btn primary" disabled={__AXON_DEMO__ || !repoPath}
            onClick={() => void run()}>Compute affected APIs</button>
          {err && <p className="f-error">{err}</p>}
        </Card>
        {result && (
          <Section title={`${result.affected_apis.length} affected API(s)`}
            sub={`in ${result.affected_services.join(", ") || "—"}`}>
            <Card>
              <div className="stack">
                {result.affected_apis.map((a) => (
                  <span key={a} className="mono t-sm">{a}</span>))}
              </div>
              {result.markdown && <pre className="codeblock" style={{ marginTop: 10 }}>{result.markdown}</pre>}
            </Card>
          </Section>
        )}
      </div>
    </Page>
  );
}

export function OnboardPage() {
  const [repoPath, setRepoPath] = useState("");
  const [report, setReport] = useState<OnboardReport | null>(null);
  const [err, setErr] = useState("");
  const run = async (): Promise<void> => {
    setErr("");
    try { setReport(await api.onboard(repoPath)); }
    catch (e) { setErr(String(e)); }
  };
  return (
    <Page>
      <PageHeader title="Onboard repo" sub="draft an axon.yaml service map by scanning" />
      <div className="page-pad stack">
        <div className="banner tone-info">
          The scan only drafts axon.yaml. Review every ambiguity, then commit it by PR.
          Agents: use the axon-onboard skill for the full workflow.
        </div>
        <Card>
          <label className="lab" htmlFor="ob-repo">Repo path on the server</label>
          <div className="row">
            <input id="ob-repo" className="field grow" value={repoPath}
              onChange={(e) => setRepoPath(e.target.value)} placeholder="/path/to/repo" />
            <button className="btn primary" disabled={__AXON_DEMO__ || !repoPath}
              onClick={() => void run()}>Scan</button>
          </div>
          {__AXON_DEMO__ && <p className="f-hint">Demo build: onboarding needs the live API.</p>}
          {err && <p className="f-error">{err}</p>}
        </Card>
        {report && (
          <>
            {report.ambiguities.map((a) => (
              <div key={a} className="banner tone-risk">{a}</div>))}
            {report.evidence && Object.keys(report.evidence).length > 0 && (
              <Section title="Why these services" sub="evidence behind each drafted root">
                <div className="stack">
                  {Object.entries(report.evidence).map(([sid, why]) => (
                    <p key={sid} className="f-hint"><strong>{sid}</strong>: {why.join("; ")}</p>))}
                </div>
              </Section>
            )}
            <Section title="Draft axon.yaml" sub="review, then commit by PR">
              <pre className="codeblock">{report.draft_yaml}</pre>
            </Section>
          </>
        )}
      </div>
    </Page>
  );
}
