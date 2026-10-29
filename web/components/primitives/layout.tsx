/* Layout — page scaffolding shared by every route: scroll page,
   header, section, card, stat, empty state, disclosure.
   New pages MUST start from these instead of re-deriving padding,
   heading sizes and card chrome inline. */
import React, { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cx } from "../../base/utils";

/** The standard scrolling page column. */
export function Page({ children, style, className }: { children: React.ReactNode; style?: React.CSSProperties; className?: string }) {
  return <div className={cx("page", className)} style={style}>{children}</div>;
}

/** Page title row: icon?, title, sub?, meta chips, actions right. */
export function PageHeader({ title, sub, icon, meta, actions, display }: {
  title: React.ReactNode; sub?: React.ReactNode; icon?: React.ReactNode;
  meta?: React.ReactNode; actions?: React.ReactNode; display?: boolean;
}) {
  return (
    <div className="page-head">
      {icon}
      <h1 className={display ? "t-display" : "t-title"}>{title}</h1>
      {sub && <span className="muted">{sub}</span>}
      {meta}
      <span className="spacer" />
      {actions}
    </div>
  );
}

/** Section heading row ("Clarified vision · kept live by the agent"). */
export function Section({ title, sub, right, children, style }: {
  title: string; sub?: string; right?: React.ReactNode;
  children?: React.ReactNode; style?: React.CSSProperties;
}) {
  return (
    <section style={style}>
      <div className="section-h">
        <h2 className="t-h2 grow">{title}{sub && <span className="sub"> {sub}</span>}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Card({ children, size, hoverable, className, style, ...rest }: React.HTMLAttributes<HTMLDivElement> & {
  size?: "md" | "lg"; hoverable?: boolean;
}) {
  return <div className={cx("card", size === "lg" && "lg", hoverable && "hoverable", className)} style={style} {...rest}>{children}</div>;
}

/** Label-over-value fact ("BEST COST / $4.20"). */
export function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div>
      <div className="t-label">{label}</div>
      <div style={{ fontSize: 16, fontWeight: 650, marginTop: 3 }}>{value}</div>
      {sub && <div className="dim t-sm" style={{ marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

/** Centred empty / not-found state. `action` is usually a <Button>. */
export function EmptyState({ title, text, action }: { title?: string; text: string; action?: React.ReactNode }) {
  return (
    <div className="empty">
      {title && <span className="empty-title">{title}</span>}
      {text}
      {action && <div style={{ marginTop: 12 }}>{action}</div>}
    </div>
  );
}

/** Collapsible rail row (extracted from blast.tsx's Row — same look). */
export function Disclosure({ label, summary, children, defaultOpen = true }: {
  label: string; summary?: string; children: React.ReactNode; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ borderTop: "1px solid var(--line-soft)" }}>
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        style={{ display: "flex", alignItems: "center", gap: 7, width: "100%", textAlign: "left", padding: "10px 0 8px" }}>
        <span style={{ width: 22, height: 22, borderRadius: "50%", border: "1px solid var(--line)", background: "var(--bg-3)", color: "var(--tx)", display: "inline-flex", alignItems: "center", justifyContent: "center", flex: "none", transform: open ? "rotate(90deg)" : "none", transition: "transform .2s var(--ease)" }}><ChevronRight size={14} strokeWidth={2.2} /></span>
        <span style={{ fontSize: 12.5, fontWeight: 550, color: "var(--tx-2)", flex: 1 }}>{label}</span>
        {!open && summary && <span className="dim" style={{ fontSize: 11.5 }}>{summary}</span>}
      </button>
      <div style={{ display: "grid", gridTemplateRows: open ? "1fr" : "0fr", transition: "grid-template-rows .26s cubic-bezier(.32,.72,0,1)" }}>
        <div style={{ overflow: "hidden" }}><div style={{ paddingBottom: 11 }}>{children}</div></div>
      </div>
    </div>
  );
}
