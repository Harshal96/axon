/* ModelPicker — copied from Arc's UI/components/domain/model-picker.tsx
   (itself reimplemented from the affectUI model-picker spec
   (https://www.affectui.com/components/model-picker, Pro source not
   used): the model menu of an AI chat. Point at a model and its speed,
   quality and cost bars fill segment by segment while the other rows
   step back; extended thinking unfolds its effort control; the tray
   rolls first-token time and price to the hovered/chosen model.
   One file, styles inside, Arc tokens only, no animation library. */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { Check, ChevronDown, Gauge, Globe, Lightbulb } from "lucide-react";

import type { Effort } from "../../base/types";
import { prefersReducedMotion as reduced, useAnimatedNumber } from "../../base/hooks";
import { fmtUsd as fmtPrice } from "../../base/utils";
export type { Effort };
export type PickerPriority = "low" | "normal" | "high";
export interface PickerModel {
  id: string; name: string; description?: string; icon?: React.ReactNode;
  speed?: number; quality?: number; cost?: number; /* 1..5, cost 5 = priciest */
  latencyMs?: number; pricePerM?: number;
}
const EFFORTS: Effort[] = ["low", "medium", "high", "max"];
const PRIORITIES: PickerPriority[] = ["low", "normal", "high"];
const EFFORT_MULT: Record<Effort, number> = { low: 1.6, medium: 2.5, high: 4, max: 6 };

/* Roll a number toward its target (base/hooks useAnimatedNumber). */
const useRolling = (target: number) => useAnimatedNumber(target, 450);
const fmtLatency = (ms: number) => ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;

function Bars({ m, filled }: { m: PickerModel; filled: boolean }) {
  if (m.speed == null && m.quality == null && m.cost == null)
    return <span className="mp-varies">Varies</span>;
  const rows: [string, number | undefined][] = [["Speed", m.speed], ["Quality", m.quality], ["Cost", m.cost]];
  return (
    <span className="mp-bars">
      {rows.map(([label, v]) => (
        <span key={label} className="mp-bar-row">
          <span className="mp-bar-label">{label}</span>
          <span className="mp-segs" role="img" aria-label={`${label} ${v ?? 0} of 5`}>
            {[0, 1, 2, 3, 4].map(i => (
              <span key={i} className="mp-seg" style={{
                transitionDelay: filled && !reduced() ? `${i * 45}ms` : "0ms",
                background: filled && v != null && i < v ? "var(--tx)" : "var(--bg-3)",
              }} />
            ))}
          </span>
        </span>
      ))}
    </span>
  );
}

function Switch({ on, onFlip, label }: { on: boolean; onFlip: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className="mp-switch"
      onClick={() => onFlip(!on)}>
      <span className="mp-knob" style={{ transform: on ? "translateX(16px)" : "translateX(0)" }} />
    </button>
  );
}

export function ModelPicker(props: {
  models: PickerModel[];
  value?: string; defaultValue?: string; onChange?: (id: string) => void;
  thinking?: boolean; defaultThinking?: boolean; onThinkingChange?: (on: boolean) => void;
  webSearch?: boolean; defaultWebSearch?: boolean; onWebSearchChange?: (on: boolean) => void;
  effort?: Effort; defaultEffort?: Effort; onEffortChange?: (e: Effort) => void;
  /* Run priority lives in the same menu for approval-card use (the card
     merges model + thinking + priority into this one control). Composer
     use leaves it hidden. */
  priority?: PickerPriority; defaultPriority?: PickerPriority; onPriorityChange?: (p: PickerPriority) => void;
  showPriority?: boolean; priorityHint?: string;
  open?: boolean; defaultOpen?: boolean; onOpenChange?: (open: boolean) => void;
  align?: "start" | "end"; side?: "bottom" | "top"; className?: string;
  /* Form contexts (e.g. the agent approval card) already own thinking /
     priority fields and have no web-search concept — hide those settings
     so the picker is purely the model menu. Default keeps composer use. */
  showThinking?: boolean; showWebSearch?: boolean;
  /* "field" stretches the trigger into a full-width form field instead of
     the compact composer pill. */
  variant?: "pill" | "field";
}) {
  const showThinking = props.showThinking ?? true;
  const showWebSearch = props.showWebSearch ?? true;
  const isField = props.variant === "field";
  const { models, align = "start", side = "bottom" } = props;
  const uid = useId().replace(/[:]/g, "");
  const [innerValue, setInnerValue] = useState(props.defaultValue ?? models[0]?.id);
  const [innerThinking, setInnerThinking] = useState(props.defaultThinking ?? false);
  const [innerSearch, setInnerSearch] = useState(props.defaultWebSearch ?? false);
  const [innerEffort, setInnerEffort] = useState<Effort>(props.defaultEffort ?? "medium");
  const [innerPriority, setInnerPriority] = useState<PickerPriority>(props.defaultPriority ?? "normal");
  const [innerOpen, setInnerOpen] = useState(props.defaultOpen ?? false);
  const [closing, setClosing] = useState(false);
  const [active, setActive] = useState(0);       /* keyboard/hover focus in the list */
  const [settled, setSettled] = useState("");    /* polite announcement, set after a roll */
  const rootRef = useRef<HTMLSpanElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeTimer = useRef(0);

  const value = props.value ?? innerValue;
  const thinking = props.thinking ?? innerThinking;
  const webSearch = props.webSearch ?? innerSearch;
  const effort = props.effort ?? innerEffort;
  const priority = props.priority ?? innerPriority;
  const showPriority = props.showPriority ?? false;
  const open = props.open ?? innerOpen;
  const selected = models.find(m => m.id === value) ?? models[0];
  const shown = models[active] ?? selected;      /* tray + bars follow hover/arrows */
  const latency = shown?.latencyMs != null ? shown.latencyMs * (showThinking && thinking ? EFFORT_MULT[effort] : 1) : null;
  const rollLatency = useRolling(latency ?? 0);
  const rollPrice = useRolling(shown?.pricePerM ?? 0);

  const setOpen = useCallback((o: boolean) => {
    window.clearTimeout(closeTimer.current);
    if (o) { setClosing(false); props.onOpenChange?.(true); if (props.open === undefined) setInnerOpen(true); }
    else if (!reduced()) {           /* play the close the same way it opened */
      setClosing(true);
      closeTimer.current = window.setTimeout(() => {
        setClosing(false); props.onOpenChange?.(false); if (props.open === undefined) setInnerOpen(false);
      }, 130);
    } else { props.onOpenChange?.(false); if (props.open === undefined) setInnerOpen(false); }
  }, [props]);

  useEffect(() => () => window.clearTimeout(closeTimer.current), []);
  useEffect(() => { if (open) setActive(Math.max(0, models.findIndex(m => m.id === value))); }, [open]); // eslint-disable-line
  useEffect(() => { /* announce once, when the roll has landed */
    if (latency == null || shown?.pricePerM == null) { setSettled(""); return; }
    const t = window.setTimeout(() => setSettled(`${shown.name}: first token ${fmtLatency(latency)}, $${shown.pricePerM} per million tokens`), 480);
    return () => window.clearTimeout(t);
  }, [latency, shown]);
  useEffect(() => { /* close on outside pointer */
    if (!open) return;
    const h = (e: PointerEvent) => { if (!rootRef.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", h);
    return () => document.removeEventListener("pointerdown", h);
  }, [open, setOpen]);

  const pick = (i: number) => {
    const m = models[i]; if (!m) return;
    if (props.value === undefined) setInnerValue(m.id);
    props.onChange?.(m.id); setOpen(false); triggerRef.current?.focus();
  };
  const flipThinking = (v: boolean) => { if (props.thinking === undefined) setInnerThinking(v); props.onThinkingChange?.(v); };
  const flipSearch = (v: boolean) => { if (props.webSearch === undefined) setInnerSearch(v); props.onWebSearchChange?.(v); };
  const setEffort = (e: Effort) => { if (props.effort === undefined) setInnerEffort(e); props.onEffortChange?.(e); };
  const setPriority = (p: PickerPriority) => { if (props.priority === undefined) setInnerPriority(p); props.onPriorityChange?.(p); };

  const onListKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(a => Math.min(models.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(a => Math.max(0, a - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(models.length - 1); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(active); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); triggerRef.current?.focus(); }
  };
  const origin = `${side === "top" ? "bottom" : "top"} ${align === "end" ? "right" : "left"}`;

  return (
    <span ref={rootRef} className={`mp-root ${isField ? "mp-field" : ""} ${props.className ?? ""}`}>
      <button ref={triggerRef} type="button" className="mp-trigger"
        aria-haspopup="listbox" aria-expanded={open} aria-controls={`${uid}-menu`}
        aria-label={`Model: ${selected?.name ?? ""}${showThinking && thinking ? ", extended thinking on" : ""}`}
        onClick={() => setOpen(!open)}
        onKeyDown={e => { if ((e.key === "Enter" || e.key === " " || e.key === "ArrowDown") && !open) { e.preventDefault(); setOpen(true); requestAnimationFrame(() => listRef.current?.focus()); } }}>
        <span key={selected?.id} className="mp-cross">{selected?.name}</span>
        {showThinking && <span className="mp-bulb" data-on={thinking} aria-hidden={!thinking}>
          <Lightbulb size={13} />
        </span>}
        <ChevronDown size={13} style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform .18s", color: "var(--tx-3)", flex: "none" }} />
      </button>

      {(open || closing) && (
        <div id={`${uid}-menu`} className={`mp-menu ${closing ? "mp-out" : "mp-in"}`}
          style={{ transformOrigin: origin,
            ...(side === "top" ? { bottom: "calc(100% + 6px)" } : { top: "calc(100% + 6px)" }),
            ...(align === "end" ? { right: 0 } : { left: 0 }) }}>
          <div ref={listRef} role="listbox" tabIndex={0} aria-label="Models"
            aria-activedescendant={`${uid}-m-${shown?.id}`}
            onKeyDown={onListKey} className="mp-list">
            {models.map((m, i) => {
              const isSel = m.id === value, isActive = i === active;
              return (
                <button key={m.id} id={`${uid}-m-${m.id}`} type="button" role="option" aria-selected={isSel}
                  data-dim={active !== i && (models.length > 1) ? "" : undefined}
                  className={`mp-row ${isActive ? "mp-row-on" : ""}`}
                  onPointerEnter={() => setActive(i)} onFocus={() => setActive(i)}
                  onClick={() => pick(i)}>
                  <span className="mp-row-main">
                    <span className="mp-row-name">{m.icon}{m.name}
                      {isSel && <Check size={13} style={{ color: "var(--tx-2)" }} />}</span>
                    {m.description && <span className="mp-row-desc">{m.description}</span>}
                  </span>
                  <Bars m={m} filled={isActive || isSel} />
                </button>
              );
            })}
          </div>

          {(showThinking || showWebSearch) && <div className="mp-sep" />}
          {showThinking && <>
          <div className="mp-setting">
            <span className="mp-setting-label"><Lightbulb size={13} /> Extended thinking</span>
            <Switch label="Extended thinking" on={thinking} onFlip={flipThinking} />
          </div>
          <div className="mp-effort-wrap" data-on={thinking} aria-hidden={!thinking}>
            <div className="mp-effort-inner">
              <div role="radiogroup" aria-label="Thinking effort" className="mp-effort"
                onKeyDown={e => {
                  const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                  if (d) { e.preventDefault(); setEffort(EFFORTS[Math.min(EFFORTS.length - 1, Math.max(0, EFFORTS.indexOf(effort) + d))]); }
                }}>
                <span className="mp-thumb" style={{ transform: `translateX(${EFFORTS.indexOf(effort) * 100}%)`, width: `calc((100% - 4px) / ${EFFORTS.length})` }} />
                {EFFORTS.map(e => (
                  <button key={e} type="button" role="radio" aria-checked={effort === e}
                    tabIndex={effort === e ? 0 : -1} disabled={!thinking}
                    className={effort === e ? "on" : ""} onClick={() => setEffort(e)}>
                    {e[0].toUpperCase() + e.slice(1)}</button>
                ))}
              </div>
            </div>
          </div>
          </>}
          {showWebSearch && <div className="mp-setting">
            <span className="mp-setting-label"><Globe size={13} /> Web search</span>
            <Switch label="Web search" on={webSearch} onFlip={flipSearch} />
          </div>}
          {showPriority && <>
            <div className="mp-setting" style={{ paddingBottom: 2 }}>
              <span className="mp-setting-label"><Gauge size={13} /> Priority</span>
              {props.priorityHint && <span style={{ fontSize: 11.5, color: "var(--tx-3)" }}>{props.priorityHint}</span>}
            </div>
            <div role="radiogroup" aria-label="Run priority" className="mp-effort"
              onKeyDown={e => {
                const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                if (d) { e.preventDefault(); setPriority(PRIORITIES[Math.min(PRIORITIES.length - 1, Math.max(0, PRIORITIES.indexOf(priority) + d))]); }
              }}>
              <span className="mp-thumb" style={{ transform: `translateX(${PRIORITIES.indexOf(priority) * 100}%)`, width: `calc((100% - 4px) / ${PRIORITIES.length})` }} />
              {PRIORITIES.map(p => (
                <button key={p} type="button" role="radio" aria-checked={priority === p}
                  tabIndex={priority === p ? 0 : -1}
                  className={priority === p ? "on" : ""} onClick={() => setPriority(p)}>
                  {p[0].toUpperCase() + p.slice(1)}</button>
              ))}
            </div>
          </>}

          <div className="mp-tray">
            {latency != null && shown?.pricePerM != null ? (
              <>
                <span aria-hidden="true">First token <b>{fmtLatency(rollLatency)}</b></span>
                <span className="mp-tray-dot" aria-hidden="true">·</span>
                <span aria-hidden="true"><b>{fmtPrice(rollPrice)}</b> / M tokens</span>
                {showThinking && thinking && <span className="mp-tray-note" aria-hidden="true">with thinking</span>}
              </>
            ) : <span aria-hidden="true">Speed and price vary by model</span>}
            <span className="mp-live" aria-live="polite">{open ? settled : ""}</span>
          </div>
        </div>
      )}
      <style>{MP_CSS}</style>
    </span>
  );
}

const MP_CSS = `
.mp-root{position:relative;display:inline-flex}
.mp-trigger{display:inline-flex;align-items:center;gap:5px;padding:4px 8px;border-radius:var(--r-pill);
  color:var(--tx-2);font-size:13px;transition:background .15s,color .15s;max-width:220px}
.mp-trigger:hover{background:var(--bg-hover);color:var(--tx)}
/* Field variant — full-width form trigger (approval cards, settings).
   The menu renders IN FLOW under the trigger, not as an overlay: form
   cards often sit inside overflow-hidden accordions, which would clip an
   absolutely-positioned menu. In-flow expands the card instead. */
.mp-root.mp-field{display:flex;flex-direction:column;width:100%}
.mp-field .mp-trigger{flex:none;max-width:none;justify-content:space-between;text-align:left;
  background:var(--bg-1);border:1px solid var(--line);border-radius:var(--r-s);
  padding:7px 10px;font-size:13px;color:var(--tx)}
.mp-field .mp-trigger:hover{border-color:var(--tx-3);background:var(--bg-1)}
.mp-field .mp-cross{flex:1;overflow:hidden;text-overflow:ellipsis}
.mp-field .mp-menu{position:static;width:100%;max-width:none;margin-top:6px;box-shadow:none}
.mp-cross{animation:mpCross .22s var(--ease) both;white-space:nowrap}
@keyframes mpCross{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.mp-bulb{display:inline-flex;color:var(--risk);overflow:hidden;max-width:0;opacity:0;transform:scale(.6);
  transition:max-width .24s var(--ease),opacity .2s,transform .24s var(--ease)}
.mp-bulb[data-on="true"]{max-width:16px;opacity:1;transform:scale(1)}
.mp-menu{position:absolute;width:320px;max-width:calc(100vw - 32px);z-index:70;
  background:var(--bg-2);border:1px solid var(--line);border-radius:var(--r-l);padding:6px;
  box-shadow:var(--shadow-pop)}
.mp-in{animation:mpIn .16s var(--ease) both}
.mp-out{animation:mpOut .13s ease both}
@keyframes mpIn{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:scale(1)}}
@keyframes mpOut{to{opacity:0;transform:scale(.96)}}
.mp-list{display:flex;flex-direction:column;gap:2px;outline:none}
.mp-row{display:flex;align-items:flex-start;gap:10px;width:100%;text-align:left;padding:8px;
  border-radius:var(--r-m);transition:opacity .18s,background .15s}
.mp-row[data-dim]{opacity:.45}
.mp-row-on{background:var(--bg-hover)}
.mp-row-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.mp-row-name{display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:560;color:var(--tx)}
.mp-row-desc{font-size:12px;color:var(--tx-3)}
.mp-varies{font-size:11.5px;color:var(--tx-3);font-family:var(--mono);padding-top:3px}
.mp-bars{display:flex;flex-direction:column;gap:3px;flex:none;padding-top:2px}
.mp-bar-row{display:flex;align-items:center;gap:6px}
.mp-bar-label{width:44px;font-size:10px;color:var(--tx-3);text-align:right}
.mp-segs{display:inline-flex;gap:2px}
.mp-seg{width:14px;height:4px;border-radius:2px;transition:background .18s ease}
.mp-sep{height:1px;background:var(--line-soft);margin:6px 2px}
.mp-setting{display:flex;align-items:center;gap:8px;padding:6px 8px;font-size:13px;color:var(--tx-2)}
.mp-setting-label{display:inline-flex;align-items:center;gap:7px;flex:1}
.mp-switch{width:32px;height:18px;border-radius:999px;background:var(--bg-3);border:1px solid var(--line);
  padding:1px;flex:none;transition:background .18s}
.mp-switch[aria-checked="true"]{background:var(--tx)}
.mp-knob{display:block;width:14px;height:14px;border-radius:50%;background:var(--tx-3);transition:transform .2s var(--ease),background .18s}
.mp-switch[aria-checked="true"] .mp-knob{background:var(--bg)}
.mp-effort-wrap{display:grid;grid-template-rows:0fr;transition:grid-template-rows .26s var(--ease)}
.mp-effort-wrap[data-on="true"]{grid-template-rows:1fr}
.mp-effort-inner{overflow:hidden;min-height:0}
.mp-effort{position:relative;display:flex;margin:2px 8px 6px;background:var(--bg-1);
  border:1px solid var(--line-soft);border-radius:var(--r-s);padding:2px}
.mp-effort button{flex:1;position:relative;z-index:1;padding:4px 0;font-size:12px;color:var(--tx-3);
  border-radius:5px;text-transform:capitalize;transition:color .18s}
.mp-effort button.on{color:var(--bg);font-weight:560}
.mp-effort button:disabled{cursor:default}
.mp-thumb{position:absolute;top:2px;bottom:2px;left:2px;width:calc((100% - 4px)/3);
  background:var(--tx);border-radius:5px;transition:transform .24s var(--ease)}
.mp-tray{display:flex;align-items:center;gap:6px;margin-top:6px;padding:8px;
  background:var(--bg-1);border:1px solid var(--line-soft);border-radius:var(--r-m);
  font-size:12px;color:var(--tx-3);font-family:var(--mono)}
.mp-tray b{color:var(--tx-2);font-weight:560}
.mp-tray-note{margin-left:auto;font-family:var(--font);font-size:11px}
.mp-live{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media (pointer:coarse){.mp-trigger{padding:8px 10px}.mp-row{padding:11px 8px}.mp-effort button{padding:8px 0}}
@media (max-width:340px){.mp-bar-label{display:none}}
@media (prefers-reduced-motion:reduce){.mp-in,.mp-out,.mp-cross{animation:none}
  .mp-bulb,.mp-thumb,.mp-effort-wrap,.mp-seg,.mp-knob{transition:none}}
`;
