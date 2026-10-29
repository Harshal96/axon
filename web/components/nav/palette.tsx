/* Command palette — copied from Arc's UI/components/nav/palette.tsx,
   retargeted to Axon actions (ask / index / services / APIs). */
/* Command palette — one field, everything is an action.
   Ask, index, toggle theme or sidebar, navigate, plus live search
over services and APIs.
   ⌘K opens, ↑↓ move, ↵ runs, Tab jumps between sections, Esc closes.
   Motion is calm: the panel settles in (fade + 6px rise), rows fade in
   with a short stagger, and the highlight glides rather than jumps. */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeftRight, Bot, Circle, CircleCheck, FolderPlus, Inbox,
  Keyboard, LayoutGrid, ListTodo, Moon, PanelLeft, Plus, Search, SearchX, Settings, Sun, Target,
} from "lucide-react";

export interface PalItem { icon: React.ReactNode; name: string; sub?: string; kbd?: string; right?: string; run: () => void }
export interface PalGroup { name: string; items: PalItem[] }

const keyCap: React.CSSProperties = {
  display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 20, height: 20,
  padding: "0 5px", borderRadius: 5, background: "var(--bg-3)", border: "1px solid var(--line)",
  borderBottomWidth: 2, fontFamily: "var(--mono)", fontSize: 10.5, color: "var(--tx-2)",
};

/* The matched part of a result name, lit so the eye lands on why it matched. */
function Matched({ name, q }: { name: string; q: string }) {
  if (!q) return <>{name}</>;
  const i = name.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return <>{name}</>;
  return <>{name.slice(0, i)}<span style={{ color: "var(--tx)", fontWeight: 650 }}>{name.slice(i, i + q.length)}</span>{name.slice(i + q.length)}</>;
}

export function Palette({ groups, onClose }: { groups: (q: string) => PalGroup[]; onClose: () => void }) {
  const [q, setQ] = useState(""); const [hl, setHl] = useState(0);
  const [scope, setScope] = useState<string | null>(null); // null = all sections
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const all = useMemo(() => groups(q.trim().toLowerCase()).filter(g => g.items.length), [groups, q]);
  const gs = scope ? all.filter(g => g.name === scope) : all;
  const flat = gs.flatMap(g => g.items);
  const groupStarts = useMemo(() => {
    const starts: number[] = []; let n = 0;
    for (const g of gs) { starts.push(n); n += g.items.length; }
    return starts;
  }, [gs]);
  useEffect(() => { setHl(0); }, [q, scope]);
  useEffect(() => { if (scope && !all.some(g => g.name === scope)) setScope(null); }, [all, scope]);
  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => {
    listRef.current?.querySelector('[data-hl="true"]')?.scrollIntoView({ block: "nearest" });
  }, [hl]);
  const pick = (it?: PalItem) => { if (it) { onClose(); it.run(); } };
  const jumpSection = (dir: 1 | -1) => {
    if (!groupStarts.length) return;
    const next = groupStarts.find(s => s > hl) ?? groupStarts[0];
    const prev = [...groupStarts].reverse().find(s => s < hl) ?? groupStarts[groupStarts.length - 1];
    setHl(dir === 1 ? next : prev);
  };
  let idx = -1;
  return (
    <div className="modal-wrap" style={{ paddingTop: "11vh", background: "color-mix(in srgb, #000 72%, transparent)", backdropFilter: "blur(3px)" }}
      onClick={onClose} role="dialog" aria-modal="true" aria-label="Command palette">
      <div onClick={e => e.stopPropagation()}
        style={{ width: 660, maxWidth: "94vw", background: "var(--bg-2)", border: "1px solid var(--line)", borderRadius: "var(--r-l)", overflow: "hidden", animation: "pop .18s var(--ease)", boxShadow: "0 32px 80px -16px #000d, 0 4px 16px #0008" }}>
        <div style={{ display: "flex", gap: 11, alignItems: "center", padding: "15px 18px 13px", borderBottom: "1px solid var(--line-soft)" }}>
          <Search size={18} style={{ color: "var(--tx-3)", flex: "none" }} />
          <input ref={inputRef} value={q} onChange={e => setQ(e.target.value)} placeholder="Type a command or search…"
            aria-label="Type a command or search" role="combobox" aria-expanded="true"
            aria-activedescendant={flat[hl] ? `pal-item-${hl}` : undefined}
            onKeyDown={e => {
              if (e.key === "ArrowDown") { e.preventDefault(); setHl(h => Math.min(h + 1, flat.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setHl(h => Math.max(h - 1, 0)); }
              else if (e.key === "Home") { e.preventDefault(); setHl(0); }
              else if (e.key === "End") { e.preventDefault(); setHl(Math.max(0, flat.length - 1)); }
              else if (e.key === "Tab") { e.preventDefault(); jumpSection(e.shiftKey ? -1 : 1); }
              else if (e.key === "Enter") pick(flat[hl]);
              else if (e.key === "Escape") onClose();
            }}
            style={{ flex: 1, background: "none", border: 0, outline: "none", fontSize: 15.5, color: "var(--tx)" }} />
          {q
            ? <button className="iconbtn" style={{ width: 22, height: 22 }} aria-label="Clear search" onClick={() => setQ("")}>✕</button>
            : <span style={{ display: "inline-flex", gap: 5, alignItems: "center", color: "var(--tx-3)", fontSize: 11 }}>
                <span style={keyCap}>↵</span> ENTER</span>}
        </div>

        {all.length > 1 && (
          <div style={{ display: "flex", gap: 5, padding: "9px 14px 2px", flexWrap: "wrap" }} role="group" aria-label="Filter by section">
            {[null, ...all.map(g => g.name)].map(name => (
              <button key={name ?? "all"} className={`pill ${scope === name ? "on" : ""}`} style={{ fontSize: 12 }}
                onClick={() => setScope(name)}>{name ?? "All"}</button>
            ))}
          </div>
        )}

        <div ref={listRef} role="listbox" aria-label="Results" style={{ maxHeight: "52vh", overflowY: "auto", padding: "6px 8px 10px" }}>
          {flat.length === 0 && (
            <div style={{ padding: "30px 16px 26px", textAlign: "center" }}>
              <SearchX size={20} style={{ color: "var(--tx-3)" }} />
              <p style={{ fontSize: 13.5, color: "var(--tx-2)", marginTop: 8 }}>No results for “{q}”.</p>
              <p className="dim" style={{ fontSize: 12.5, marginTop: 3 }}>Try an API path (/v1/charges), a service name, or a command like “ask”.</p>
            </div>
          )}
          {gs.map(g => (
            <div key={g.name} style={{ marginBottom: 4 }}>
              <div style={{ fontSize: 11.5, color: "var(--tx-3)", padding: "10px 10px 5px" }}>{g.name}</div>
              {g.items.map(it => { idx++; const mine = idx === hl; const myIdx = idx; return (
                <button key={g.name + it.name} id={`pal-item-${myIdx}`} role="option" aria-selected={mine} data-hl={mine || undefined}
                  onMouseEnter={() => setHl(myIdx)} onClick={() => pick(it)}
                  className="rise"
                  style={{ display: "flex", gap: 11, alignItems: "center", width: "100%", textAlign: "left", padding: "7px 9px", borderRadius: "var(--r-m)", fontSize: 13.5,
                    animationDelay: `${Math.min(myIdx, 12) * 18}ms`,
                    background: mine ? "var(--bg-3)" : "transparent", color: mine ? "var(--tx)" : "var(--tx-2)",
                    transition: "background .12s var(--ease), color .12s var(--ease)" }}>
                  <span aria-hidden style={{ width: 30, height: 30, flex: "none", display: "inline-flex", alignItems: "center", justifyContent: "center",
                    borderRadius: 8, background: mine ? "var(--bg-2)" : "var(--bg-1)",
                    border: "1px solid var(--line-soft)", color: mine ? "var(--tx)" : "var(--tx-3)", transition: "background .12s var(--ease), color .12s var(--ease)" }}>{it.icon}</span>
                  <span style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    <Matched name={it.name} q={q.trim()} />
                    {it.sub && <span className="dim" style={{ fontSize: 12 }}> · {it.sub}</span>}</span>
                  {it.right && <span className="mono dim" style={{ fontSize: 11.5 }}>{it.right}</span>}
                  {it.kbd && <span style={{ display: "inline-flex", gap: 3 }}>{it.kbd.split(" ").map((k, i) =>
                    <span key={i} style={keyCap}>{k}</span>)}</span>}
                  {mine && !it.kbd && <span aria-hidden style={{ color: "var(--tx-3)", fontSize: 12 }}>↵</span>}
                </button>); })}
            </div>
          ))}
        </div>

        <div style={{ display: "flex", gap: 7, alignItems: "center", padding: "9px 16px", borderTop: "1px solid var(--line-soft)", fontSize: 11.5, color: "var(--tx-3)" }}>
          <span style={keyCap}>↑</span><span style={keyCap}>↓</span><span style={{ marginRight: 8 }}>to navigate</span>
          <span style={keyCap}>Tab</span><span style={{ marginRight: 8 }}>to jump between sections</span>
          <span style={keyCap}>↵</span><span>to run</span>
          <span style={{ flex: 1 }} />
          <span className="mono">{flat.length} result{flat.length === 1 ? "" : "s"}</span>
        </div>
      </div>
    </div>
  );
}
export const palIcons = {
  plus: <Plus size={15} />, project: <FolderPlus size={15} />, ws: <ArrowLeftRight size={15} />,
  search: <Search size={15} />, sun: <Sun size={15} />, moon: <Moon size={15} />,
  overview: <LayoutGrid size={15} />, inbox: <Inbox size={15} />, my: <ListTodo size={15} />,
  init: <Target size={15} />, agent: <Bot size={15} />, side: <PanelLeft size={15} />,
  settings: <Settings size={15} />, keys: <Keyboard size={15} />,
  todo: <Circle size={14} />, done: <CircleCheck size={14} style={{ color: "var(--ok)" }} />,
};
