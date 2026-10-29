/* TopBar — breadcrumb chrome above every page (copied from Arc). */
import React from "react";
import { PanelLeft, Search } from "lucide-react";

export function TopBar({ crumb, right, collapsed, setCollapsed, onPalette }: { crumb: string[]; right?: React.ReactNode; collapsed?: boolean; setCollapsed?: (c: boolean) => void; onPalette?: () => void }) {
  return (
    <header style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 18px", borderBottom: "1px solid var(--line-soft)" }}>
      {setCollapsed && <button className="iconbtn" onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
        <PanelLeft size={16} strokeWidth={1.8} style={{ transform: collapsed ? "rotate(180deg)" : "none", transition: "transform .3s" }} /></button>}
      <span style={{ fontSize: 13.5 }}>{crumb.map((c, i) => (
        <span key={i}>{i > 0 && <span className="dim"> › </span>}<span style={i === crumb.length - 1 ? { color: "var(--tx)" } : { color: "var(--tx-2)" }}>{c}</span></span>
      ))}</span>
      <span className="dim">☆</span>
      <span style={{ flex: 1 }} />
      {onPalette && <button className="searchbtn" onClick={onPalette} aria-label="Search" title="Search (⌘K)">
        <Search size={14} strokeWidth={1.8} /><span>Search</span><kbd>⌘K</kbd></button>}
      {right}
    </header>
  );
}
