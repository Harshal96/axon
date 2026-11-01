import { useEffect, useId, useRef, useState } from "react";
import type { Facet } from "@base/types";

/** Searchable multi-selection with a roving listbox focus and native keyboard keys. */
export function MultiSelect({ label, options, value, onChange }: {
  label: string; options: Facet[]; value: string[]; onChange: (value: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const id = useId();
  const shown = options.filter(o => o.label.toLowerCase().includes(q.toLowerCase()));
  const close = () => { setOpen(false); button.current?.focus(); };
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  const move = (index: number) => {
    const choices = root.current?.querySelectorAll<HTMLButtonElement>('[role="option"]');
    if (choices?.length) choices[(index + choices.length) % choices.length]?.focus();
  };
  return <div className="multi-select" ref={root} onKeyDown={e => {
    if (e.key === "Escape" && open) { e.stopPropagation(); close(); }
  }} onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}>
    <button ref={button} className={`btn ${value.length ? "active" : ""}`} type="button"
      aria-haspopup="listbox" aria-expanded={open} aria-controls={id}
      onClick={() => { setQ(""); setOpen(!open); }}
      onKeyDown={e => { if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); } }}>
      {label}{value.length ? ` · ${value.length}` : ""}<span aria-hidden>⌄</span>
    </button>
    {open && <div className="multi-popover">
      <input ref={search} className="field filter-q" value={q} aria-label={`Search ${label}`}
        placeholder={`Search ${label.toLowerCase()}…`} onChange={e => setQ(e.target.value)}
        onKeyDown={e => { if (e.key === "ArrowDown") { e.preventDefault(); move(0); } }} />
      <div id={id} className="multi-options" role="listbox" aria-label={label} aria-multiselectable="true">
        {shown.map((option, i) => <button key={option.key} type="button" role="option"
          aria-selected={value.includes(option.key)} className="multi-option"
          onClick={() => onChange(value.includes(option.key)
            ? value.filter(v => v !== option.key) : [...value, option.key])}
          onKeyDown={e => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault(); move(i + (e.key === "ArrowDown" ? 1 : -1));
            }
            if (e.key === "Home" || e.key === "End") {
              e.preventDefault(); move(e.key === "Home" ? 0 : shown.length - 1);
            }
          }}>
          <span className="multi-check" aria-hidden>{value.includes(option.key) ? "✓" : ""}</span>
          <span className="grow">{option.label}</span><span className="dim num">{option.count}</span>
        </button>)}
        {shown.length === 0 && <p className="dim t-sm">No matching options.</p>}
      </div>
    </div>}
  </div>;
}
