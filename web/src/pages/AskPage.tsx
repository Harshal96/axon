/* Ask answers use structured graph rows and source evidence. */
import { useEffect, useRef, useState } from "react";
import { Composer, StreamBullets, type PickerModel } from "@components";
import { usePrefersReducedMotion } from "@base/hooks";
import type { AskResponse, Citation, Route } from "@base/types";
import * as api from "@client";
import type { AskEngine } from "@client";
import { citationParts, formatDuration, interpretation, toFacts } from "../lib/ask-format";

export const AXON_ENGINES: PickerModel[] = [
  { id: "auto", name: "Auto", description: "Deterministic first, model second" },
  { id: "deterministic", name: "Deterministic", description: "Graph intents only — never a model", speed: 5, quality: 4, cost: 1 },
  { id: "model", name: "Model", description: "Drafts a read-only graph query (server LLM)", speed: 2, quality: 4, cost: 4 },
];
const EXAMPLES = ["What services does POST /checkout call?", "Who calls POST /v1/charges?", "Which APIs include the function price_cart?"];
const SKILLS = ["Service dependencies", "Callers", "Outbound APIs", "Functions → APIs", "Deprecation", "Catalog listings"];
interface Msg { who: "me" | "agent"; text?: string; resp?: AskResponse; secs?: number }

function CitationLabel({ citation }: { citation: Citation }) {
  const part = citationParts(citation);
  return <>{part.method && <span className={`chip method ${part.method.toLowerCase()}`}>{part.method}</span>}
    <span className="ask-path">{part.path}</span>{part.service && <span className="ask-service">{part.service}</span>}</>;
}

function Answer({ response, secs, streaming, openCitation }: {
  response: AskResponse; secs?: number; streaming: boolean; openCitation: (c: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const rows = response.rows ?? [];
  const facts = toFacts(response.answer);
  const summary = response.summary || facts.headline;
  const spots = response.blind_spots ?? [];
  const interpreted = interpretation(response.bindings);
  return <div className="ask-answer">
    <div className="ask-summary"><StreamBullets lines={[summary]} streaming={streaming} /></div>
    {rows.length > 0 ? <div className="ask-rows" aria-label="Graph answer rows">
      {(expanded ? rows : rows.slice(0, 8)).map((row, i) => {
        const location = row.via?.file ? `${row.via.file}:${row.via.line}` : row.source.file ? `${row.source.file}:${row.source.line}` : '';
        return <div className="ask-row" title={row.confidence ? `Confidence: ${row.confidence}` : undefined} key={`${row.source.node_id}-${row.target?.node_id}-${i}`}>
          <div className="ask-row-main">
            <button className="ask-api" onClick={() => openCitation(row.source)}><CitationLabel citation={row.source} /></button>
            {row.target && <><span className="ask-arrow" aria-label="calls">→</span><button className="ask-api" onClick={() => openCitation(row.target!)}><CitationLabel citation={row.target} /></button></>}
          </div>
          {(location || row.rule) && <div className="ask-location" title={`${location}${row.rule ? ` · ${row.rule}` : ''}`}>
            {row.via && location ? <button onClick={() => openCitation(row.via!)}>{location}</button> : location}
            {row.rule && ` · ${row.rule}`}
          </div>}
        </div>;
      })}
      {rows.length > 8 && <button className="ask-show" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? 'Show fewer' : `Show all ${rows.length}`}</button>}
    </div> : <>
      {!spots.length && facts.bullets.length > 0 && <StreamBullets lines={facts.bullets} streaming={streaming} />}
      {response.citations.length > 0 && <div className="ask-rows" aria-label="Citations">{response.citations.map(c => <button key={c.node_id} className="ask-citation" onClick={() => openCitation(c)} title={c.file ? `${c.file}:${c.line}` : c.node_id}>
        <CitationLabel citation={c} />{c.file && <span className="ask-location">{c.file}:{c.line}</span>}
      </button>)}</div>}
    </>}
    {spots.length > 0 && <details className="ask-disclosure" open={rows.length === 0 ? true : undefined}>
      <summary>Not visible to static analysis ({spots.length})</summary>
      <StreamBullets lines={spots} streaming={false} />
    </details>}
    <details className="ask-disclosure ask-metadata">
      <summary>{interpreted && `Interpreted as ${interpreted} · `}{response.model_used ? 'Model' : 'Deterministic'}
        {secs !== undefined && ` · ${formatDuration(secs)}`}{!response.grounded && <span className="ask-attention"> · not grounded</span>}
        {response.intent === 'ambiguous' && <span className="ask-attention"> · ambiguous</span>} · Details</summary>
      <div className="ask-detail">Intent: {response.intent}</div>
      {response.cypher && <pre className="codeblock">{response.cypher}</pre>}
      {response.evidence.length > 0 && <pre className="codeblock">{JSON.stringify(response.evidence, null, 2)}</pre>}
    </details>
  </div>;
}

export function AskPage({ initial, openApi, go }: { initial: string; openApi: (id: string) => void; go: (r: Route) => void }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [secs, setSecs] = useState(0);
  const [engineId, setEngineId] = useState<AskEngine>("auto");
  const sentInitial = useRef(false);
  const sending = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);
  const reducedMotion = usePrefersReducedMotion();
  useEffect(() => {
    if (!busy) { setSecs(0); return; }
    const started = performance.now();
    const timer = setInterval(() => setSecs((performance.now() - started) / 1000), 100);
    return () => clearInterval(timer);
  }, [busy]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: reducedMotion ? "instant" : "smooth", block: "end" }); }, [msgs, busy, reducedMotion]);
  const send = async (text: string) => {
    const q = text.trim();
    if (!q || sending.current) return;
    sending.current = true;
    setMsgs(m => [...m, { who: 'me', text: q }]);
    setBusy(true);
    const started = performance.now();
    try {
      const resp = await api.ask(q, engineId);
      setMsgs(m => [...m, { who: 'agent', resp, secs: (performance.now() - started) / 1000 }]);
    } catch (error) { setMsgs(m => [...m, { who: 'agent', text: `Ask failed: ${String(error)}` }]); }
    finally { sending.current = false; setBusy(false); }
  };
  useEffect(() => {
    if (initial && !sentInitial.current) { sentInitial.current = true; void send(initial); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);
  const openCitation = (citation: Citation) => {
    if (citation.node_id.startsWith('endpoint::')) openApi(citation.node_id);
    else if (citation.node_id.startsWith('service::')) go({ kind: 'service', id: citation.node_id.split('::').pop() ?? '' });
    else go({ kind: 'function', q: citation.label });
  };
  return <div className="ask-page"><div className={`ask-content ${msgs.length ? '' : 'ask-empty'}`}>
    {!msgs.length && <div className="ask-intro"><h1>Ask about your APIs</h1><p>Answers from indexed code, with source evidence.</p></div>}
    {msgs.map((msg, i) => msg.who === 'me' ? <div key={i} className="ask-bubble">{msg.text}</div>
      : <div key={i}>{msg.resp ? <Answer response={msg.resp} secs={msg.secs} streaming={i === msgs.length - 1} openCitation={openCitation} /> : <p className="ask-attention" role="alert">{msg.text}</p>}</div>)}
    {busy && <div className="ask-thinking" role="status">Querying the graph · {formatDuration(secs)}</div>}
    <div className="ask-composer"><Composer big={!msgs.length} autoFocus placeholder="Ask Axon…" onSend={t => void send(t)} disabled={busy}
      model={{ models: AXON_ENGINES, modelId: engineId, onModelChange: id => setEngineId(id as AskEngine), pickerSide: msgs.length ? 'top' : 'bottom' }} /></div>
    {!msgs.length && <div className="ask-examples">{EXAMPLES.map(q => <button key={q} onClick={() => void send(q)}>{q}</button>)}
      <details className="ask-disclosure"><summary>Skills</summary><p>{SKILLS.join(' · ')}</p></details>
      <p className="ask-footnote">{__AXON_DEMO__ ? 'Demo build — answers need the live API' : 'Answers come only from the indexed graph'}</p>
    </div>}
    <div ref={endRef} />
  </div></div>;
}
