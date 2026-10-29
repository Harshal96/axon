/* Agentic components — copied from Arc's UI/components/domain/agent.tsx
   (patterns learned from beautifului.dev: Thinking trace, Streaming
   bullets, Tool chips, Chat composer) and adapted to Axon:
   - Composer sends on Enter as well as Arc's ⌘↵; the attach button is
     dropped (nothing to attach in Axon) and voice uses real
     SpeechRecognition only — no simulated dictation.
   - ThinkingTrace takes a label; StreamBullets is plain strings
     (Axon answers are one fact per line, no VisionPoint model). */
import { useEffect, useRef, useState } from "react";
import { ArrowUp, Mic, Wrench } from "lucide-react";
import { usePrefersReducedMotion } from "../../base/hooks";
import { ModelPicker, type Effort, type PickerModel } from "./model-picker";

export interface ComposerModelProps {
  models: PickerModel[]; modelId?: string; onModelChange?: (id: string) => void;
  thinking?: boolean; onThinkingChange?: (on: boolean) => void;
  effort?: Effort; onEffortChange?: (e: Effort) => void;
  webSearch?: boolean; onWebSearchChange?: (on: boolean) => void;
  pickerSide?: "top" | "bottom";
}
export function Composer({ placeholder, big, onSend, autoFocus, model, disabled }: {
  placeholder: string; big?: boolean; onSend: (text: string) => void; autoFocus?: boolean;
  model?: ComposerModelProps; disabled?: boolean;
}) {
  const [v, setV] = useState(""); const [listening, setListening] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const send = () => { const t = v.trim(); if (t && !disabled) { onSend(t); setV(""); } };
  const voice = () => {
    interface SR {
      lang: string; start: () => void;
      onresult: ((e: { results: { 0: { 0: { transcript: string } } } }) => void) | null;
      onend: (() => void) | null;
    }
    const w = window as unknown as {
      SpeechRecognition?: new () => SR; webkitSpeechRecognition?: new () => SR;
    };
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) return; // no speech recognition in this browser — typing only
    const r = new Ctor(); r.lang = "en-US"; setListening(true);
    r.onresult = (e) => setV(s => s + e.results[0][0].transcript);
    r.onend = () => setListening(false); r.start();
  };
  return (
    <div style={{ background: "var(--bg-2)", border: "1px solid var(--line)", borderRadius: "var(--r-l)", padding: big ? "14px 16px 10px" : "10px 14px 8px" }}>
      <textarea ref={ref} autoFocus={autoFocus} value={v} onChange={e => setV(e.target.value)} rows={big ? 3 : 2}
        placeholder={placeholder} aria-label="Ask a question"
        onKeyDown={e => {
          if (e.key === "Enter" && ((e.metaKey || e.ctrlKey) || !e.shiftKey)) {
            e.preventDefault(); send();
          }
        }}
        style={{ width: "100%", background: "none", border: 0, resize: "none", outline: "none", fontSize: big ? 16 : 14 }} />
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        {model ? (
          <ModelPicker models={model.models} value={model.modelId} onChange={model.onModelChange}
            thinking={model.thinking} onThinkingChange={model.onThinkingChange}
            effort={model.effort} onEffortChange={model.onEffortChange}
            webSearch={model.webSearch} onWebSearchChange={model.onWebSearchChange}
            showThinking={false} showWebSearch={false}
            side={model.pickerSide ?? "top"} />
        ) : <span className="chip">↵ send</span>}
        <span style={{ flex: 1 }} />
        <button className="iconbtn" onClick={voice} aria-label="Voice input" title="Voice input"
          style={listening ? { color: "var(--off)", borderColor: "var(--off)" } : undefined}>
          <Mic size={15} />
        </button>
        <button onClick={send} disabled={!v.trim() || disabled} aria-label="Send"
          style={{ width: 28, height: 28, borderRadius: "50%", background: v.trim() && !disabled ? "#e8e8ea" : "var(--bg-3)", color: "#0b0b0c", display: "inline-flex", alignItems: "center", justifyContent: "center", transition: "background .15s" }}><ArrowUp size={15} strokeWidth={2.2} /></button>
      </div>
      {listening && <div style={{ fontSize: 12, color: "var(--off)", marginTop: 4 }}>● Listening…</div>}
    </div>
  );
}

export function ThinkingTrace({ steps, secs, label = "Querying the graph" }: {
  steps: string[]; secs: number; label?: string;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="rise" style={{ fontSize: 13, color: "var(--tx-2)", margin: "10px 0" }}>
      <button onClick={() => setOpen(!open)} style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--tx-3)" }} aria-expanded={open}>
        <span style={{ display: "inline-flex", gap: 3 }}>{[0, 1, 2].map(i =>
          <span key={i} style={{ width: 5, height: 5, borderRadius: 1, background: "var(--accent)", animation: `blink 1s ${i * .18}s infinite` }} />)}</span>
        {label} · {secs.toFixed(1)}s <span style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .2s", display: "inline-block" }}>›</span>
      </button>
      {open && <div style={{ margin: "8px 0 0 6px", paddingLeft: 14, borderLeft: "1px solid var(--line)" }}>
        {steps.map((s, i) => <div key={i} className="rise" style={{ padding: "2px 0", animationDelay: `${i * 60}ms` }}>
          <span className="dim mono" style={{ fontSize: 11, marginRight: 8 }}>{String(i + 1).padStart(2, "0")}</span>{s}</div>)}
      </div>}
      <style>{`@keyframes blink{0%,100%{opacity:.25}50%{opacity:1}}`}</style>
    </div>
  );
}

export function StreamBullets({ lines, streaming }: { lines: string[]; streaming: boolean }) {
  const reducedMotion = usePrefersReducedMotion();
  const stream = streaming && !reducedMotion;
  const [shown, setShown] = useState(stream ? 0 : lines.length);
  useEffect(() => {
    if (!stream) { setShown(lines.length); return; }
    setShown(0);
    const t = setInterval(() => setShown(s => { if (s >= lines.length) { clearInterval(t); return s; } return s + 1; }), 260);
    return () => clearInterval(t);
  }, [lines, stream]);
  return (
    <ul style={{ listStyle: "none" }}>
      {lines.slice(0, shown).map((l, i) => (
        <li key={i} className="rise" style={{ padding: "3px 0 3px 18px", position: "relative", fontSize: 14.5, lineHeight: 1.55 }}>
          <span style={{ position: "absolute", left: 4, top: 11, width: 5, height: 5, borderRadius: "50%", background: "var(--accent)" }} />
          <span>{l}</span>
        </li>))}
      {stream && shown < lines.length && <span style={{ color: "var(--accent)", animation: "blink 1s infinite" }}>▍</span>}
    </ul>
  );
}

export function ToolChips({ items }: { items: string[] }) {
  return <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "8px 0" }}>
    {items.map(t => <span key={t} className="chip"><Wrench size={11} /> {t}</span>)}</div>;
}
