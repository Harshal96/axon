/* Ask Axon — the agent chat page, copied from Arc's AgentPage
   (Linear's "New chat": a centred Composer with a model picker,
   Skills row, and example cards). Here the agent answers from the
   indexed graph: deterministic intents first, a model-drafted
   read-only query second, one line per fact, citations always. */
import { useEffect, useRef, useState } from "react";
import {
  Archive, Box, ChevronDown, ScanSearch, Search, Sparkles,
} from "lucide-react";
import {
  Composer, StreamBullets, ThinkingTrace, type PickerModel,
} from "@components";
import type { AskResponse, Route } from "@base/types";
import * as api from "@client";
import type { AskEngine } from "@client";

/** Real engines (axon/v2/ask.py): the picker chooses how the server
 *  answers, it is not decoration. Bars are qualitative 1..5. */
export const AXON_ENGINES: PickerModel[] = [
  { id: "auto", name: "Auto", description: "Deterministic first, model second" },
  { id: "deterministic", name: "Deterministic", description: "Graph intents only — never a model", speed: 5, quality: 4, cost: 1 },
  { id: "model", name: "Model", description: "Drafts a read-only graph query (server LLM)", speed: 2, quality: 4, cost: 4 },
];

/** What the deterministic layer understands (the Skills panel). */
const SKILLS: { intent: string; blurb: string; say: string }[] = [
  { intent: "deprecatable", blurb: "Can an API or function be deprecated?", say: "Can GET /v1/legacy be deprecated?" },
  { intent: "callers", blurb: "Who calls an API, with file and line", say: "Who calls POST /v1/charges?" },
  { intent: "outbound", blurb: "What an API's handler code calls", say: "What services does POST /checkout call?" },
  { intent: "service_outbound", blurb: "What a whole service calls", say: "What services does checkout call?" },
  { intent: "function_apis", blurb: "Which APIs include a function", say: "Which APIs is the function price_cart a part of?" },
  { intent: "list_apis / list_services", blurb: "Catalog listings", say: "List the APIs in payment" },
];

interface Msg {
  who: "me" | "agent";
  text?: string;
  headline?: string;
  bullets?: string[];
  resp?: AskResponse;
  secs?: number;
}

/** One line per fact: first sentence is the headline, the rest stream
 *  as bullets. Split on sentence boundaries only, so file paths like
 *  app.py:14 are never broken apart. */
function toFacts(answer: string): { headline?: string; bullets: string[] } {
  const parts = answer.split(/(?<=[.!?])\s+(?=[A-Z(])/).map((s) => s.trim()).filter(Boolean);
  if (parts.length <= 1) return { bullets: parts.length ? parts : [answer] };
  return { headline: parts[0], bullets: parts.slice(1) };
}

const TRACE_STEPS = [
  "Matching deterministic intents",
  "Querying the indexed graph",
  "Grounding the answer in citations",
];

export function AskPage({ initial, openApi, go }: {
  initial: string;
  openApi: (id: string) => void; go: (r: Route) => void;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const [secs, setSecs] = useState(0);
  const [engineId, setEngineId] = useState<AskEngine>("auto");
  const [skillsOpen, setSkillsOpen] = useState(false);
  const sentInitial = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!busy) { setSecs(0); return; }
    const t0 = performance.now();
    const t = setInterval(() => setSecs((performance.now() - t0) / 1000), 100);
    return () => clearInterval(t);
  }, [busy]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [msgs, busy]);

  const send = async (text: string): Promise<void> => {
    const q = text.trim();
    if (!q || busy) return;
    setSkillsOpen(false);
    setMsgs((m) => [...m, { who: "me", text: q }]);
    setBusy(true);
    const t0 = performance.now();
    try {
      const resp = await api.ask(q, engineId);
      const facts = toFacts(resp.answer);
      setMsgs((m) => [...m, {
        who: "agent", ...facts, resp,
        secs: (performance.now() - t0) / 1000,
      }]);
    } catch (e) {
      setMsgs((m) => [...m, { who: "agent", bullets: [`Ask failed: ${String(e)}`] }]);
    } finally { setBusy(false); }
  };
  useEffect(() => {
    if (initial && !sentInitial.current) {
      sentInitial.current = true;
      void send(initial);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  const openCitation = (nodeId: string, label: string) => {
    if (nodeId.startsWith("endpoint::")) openApi(nodeId);
    else if (nodeId.startsWith("service::")) go({ kind: "service", id: nodeId.split("::").pop() ?? "" });
    else go({ kind: "function", q: label });
  };

  const examples = [
    { icon: <Archive size={15} />, title: "Check a deprecation", sub: "Callers and the spec flag, with file and line", say: "Can GET /v1/legacy be deprecated?" },
    { icon: <Search size={15} />, title: "Trace who calls an API", sub: "Every indexed caller of one endpoint", say: "Who calls POST /v1/charges?" },
    { icon: <ScanSearch size={15} />, title: "Map a function's APIs", sub: "Reverse lookup from a symbol to its APIs", say: "Which APIs is the function price_cart a part of?" },
  ];
  return (
    <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column" }}>
      <div style={{ flex: 1, width: "min(760px,94vw)", margin: "0 auto", padding: msgs.length ? "26px 0 8px" : "16vh 0 8px" }}>
        {!msgs.length && <div style={{ textAlign: "center", color: "var(--tx-3)", fontSize: 13, marginBottom: 14 }}>
          <Sparkles size={15} style={{ verticalAlign: -2 }} /> Ask Axon about the graph — it answers from indexed code, one line per fact, with citations.
        </div>}
        {msgs.map((m, i) => m.who === "me"
          ? <div key={i} className="rise" style={{ alignSelf: "flex-end", marginLeft: "auto", maxWidth: "75%", background: "var(--bg-3)", border: "1px solid var(--line-soft)", borderRadius: "var(--r-l)", padding: "10px 14px", margin: "8px 0 8px auto", fontSize: 14 }}>{m.text}</div>
          : <div key={i} className="rise" style={{ margin: "10px 0", maxWidth: "92%" }}>
              {m.headline && <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 14, marginBottom: 6 }}>
                <Sparkles size={14} style={{ color: "var(--accent)", flex: "none" }} /><b>{m.headline}</b></div>}
              {m.bullets && <StreamBullets lines={m.bullets} streaming={i === msgs.length - 1} />}
              {m.resp && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8, alignItems: "center" }}>
                  <span className="chip tone-accent">{m.resp.intent}</span>
                  <span className={`chip ${m.resp.model_used ? "tone-info" : "tone-ok"}`}>
                    {m.resp.model_used ? "model" : "deterministic · no model"}</span>
                  <span className={`chip ${m.resp.grounded ? "" : "tone-off"}`}>
                    {m.resp.grounded ? "grounded in graph" : "not grounded"}</span>
                  {m.secs !== undefined && <span className="dim" style={{ fontSize: 12 }}>{m.secs < 1 ? `${Math.max(1, Math.round(m.secs * 1000))}ms` : `${m.secs.toFixed(1)}s`}</span>}
                </div>)}
              {m.resp && m.resp.citations.length > 0 && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
                  {m.resp.citations.slice(0, 10).map((c) => (
                    <button key={c.node_id} className="chip cite"
                      title={c.file ? `${c.file}:${c.line}` : c.node_id}
                      onClick={() => openCitation(c.node_id, c.label)}>
                      {c.label}{c.file ? ` · ${c.file}:${c.line}` : ""}
                    </button>))}
                </div>)}
              {m.resp?.cypher && (
                <details style={{ marginTop: 10 }}>
                  <summary className="dim t-sm">Generated query (read-only)</summary>
                  <pre className="codeblock">{m.resp.cypher}</pre>
                </details>)}
              {m.resp && m.resp.evidence.length > 0 && (
                <details style={{ marginTop: 6 }}>
                  <summary className="dim t-sm">Evidence rows</summary>
                  <pre className="codeblock">{JSON.stringify(m.resp.evidence, null, 2)}</pre>
                </details>)}
            </div>)}
        {busy && <ThinkingTrace steps={TRACE_STEPS} secs={secs} />}
        <div style={{ marginTop: 14 }}>
          <Composer big={!msgs.length} autoFocus placeholder="Ask Axon…"
            onSend={(t) => void send(t)} disabled={busy}
            model={{ models: AXON_ENGINES, modelId: engineId,
              onModelChange: (id) => setEngineId(id as AskEngine),
              pickerSide: msgs.length ? "top" : "bottom" }} />
          <div style={{ display: "flex", alignItems: "center", marginTop: 8 }}>
            <button className="pill" aria-expanded={skillsOpen}
              onClick={() => setSkillsOpen((o) => !o)}>
              <Box size={13} /> Skills <ChevronDown size={12}
                style={{ transform: skillsOpen ? "rotate(180deg)" : "none", transition: "transform .18s" }} />
            </button>
            <span style={{ flex: 1 }} />
            <span className="dim" style={{ fontSize: 12 }}>
              {__AXON_DEMO__
                ? "Demo build — answers need the live API"
                : "Answers come only from the indexed graph · citations always"}
            </span>
          </div>
          {skillsOpen && (
            <div className="card rise" style={{ marginTop: 8, padding: "8px 10px" }}
              aria-label="Deterministic skills">
              <div className="t-label" style={{ padding: "4px 6px 8px" }}>
                Deterministic skills — no model needed. Click one to ask it.
              </div>
              {SKILLS.map((s) => (
                <button key={s.intent} className="attn-row" onClick={() => void send(s.say)}>
                  <span className="chip tone-accent">{s.intent}</span>
                  <span className="grow">
                    {s.blurb}<span className="dim"> — “{s.say}”</span>
                  </span>
                </button>))}
            </div>)}
        </div>
        {!msgs.length && <>
          <div style={{ fontSize: 12.5, color: "var(--tx-3)", margin: "20px 0 8px" }}>Get started with some examples</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(210px,1fr))", gap: 10 }}>
            {examples.map(e => (
              <button key={e.title} onClick={() => void send(e.say)} style={{ textAlign: "left", border: "1px solid var(--line-soft)", background: "var(--bg-1)", borderRadius: "var(--r-m)", padding: "14px 15px", minHeight: 112, display: "flex", flexDirection: "column" }} className="excard">
                <span style={{ color: "var(--tx-3)" }}>{e.icon}</span>
                <span style={{ flex: 1 }} />
                <span style={{ fontSize: 13.5, fontWeight: 560, display: "block" }}>{e.title}</span>
                <span style={{ fontSize: 12.5, color: "var(--tx-3)", display: "block", marginTop: 3 }}>{e.sub}</span>
              </button>
            ))}
          </div>
          <style>{`.excard:hover{border-color:var(--line);background:var(--bg-2)}`}</style>
        </>}
        <div ref={endRef} />
      </div>
    </div>
  );
}
