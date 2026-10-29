/* Nav — sidebar copied from Arc's UI/components/nav/sidebar.tsx
   (gr8r anatomy: top-left switcher popover, Lucide icons, grouped
   items, 54px folding collapse) and adapted to Axon: the workspace
   switcher lists indexed repos, PROJECTS become SERVICES (expandable
   to their APIs), and the profile popover becomes the connection +
   theme popover. Classes are Arc's, unchanged. */
import React, { useEffect, useRef, useState } from "react";
import {
  Bot, Boxes, ChevronRight, ChevronsUpDown, FolderGit2, GitPullRequest,
  Keyboard, Monitor, Moon, Palette, Plus, RefreshCw, ScanSearch,
  Sun, Waypoints,
} from "lucide-react";
import { HUE_SWATCHES, hexToHue, hueToHex, swatchColor, type Theme } from "../../base/ui";
export type { Theme };
import type { GNode, Route } from "../../base/types";

export type { Route };

const repoColors = ["#2f9e6e", "#3e8ef7", "#d45d8a", "#e3925f", "#7a5af5"];
export const repoColor = (i: number) => repoColors[i % repoColors.length];

function Item({ on, onClick, icon, label, count, pill, collapsed }: {
  on?: boolean; onClick?: () => void; icon: React.ReactNode; label: string;
  count?: string; pill?: boolean; collapsed: boolean;
}) {
  return (
    <button className={`sitem ${on ? "on" : ""}`} onClick={onClick} title={collapsed ? label : undefined}>
      <span className="ic">{icon}</span>
      <span className="lbl">{label}</span>
      {count && <span className={`ct ${pill ? "pillct" : ""}`}>{count}</span>}
    </button>
  );
}

export function Sidebar({ services, apis, repos, route, go, connected, collapsed,
  theme, setTheme, hue, setHue, onPalette, onReconnect }: {
  services: GNode[]; apis: GNode[]; repos: string[];
  route: Route; go: (r: Route) => void;
  /* /api/v2 probe result (always true in the demo build). */
  connected: boolean;
  collapsed: boolean;
  theme: Theme; setTheme: (t: Theme) => void; onPalette: () => void;
  /* Colour mode: the user's hint hue (0-359) + setter (Arc THEME.md) */
  hue: number; setHue: (h: number) => void;
  onReconnect: () => void;
}) {
  const [pop, setPop] = useState(false);
  const [footPop, setFootPop] = useState(false);
  const popRef = useRef<HTMLDivElement>(null);
  const footRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (!popRef.current?.contains(e.target as Node)) setPop(false);
      if (!footRef.current?.contains(e.target as Node)) setFootPop(false);
    };
    document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h);
  }, []);
  const i16 = { size: 16, strokeWidth: 1.8 } as const;
  const deprecated = apis.filter((a) => a.props.deprecated === "true").length;
  const openService = route.kind === "service" ? route.id
    : route.kind === "apis" ? (route.service ?? null) : null;
  /* Services auto-expand when opened — a folded service stays folded
     until its chevron is clicked again (Arc's project rule). */
  const [folded, setFolded] = useState<ReadonlySet<string>>(new Set());
  const toggle = (id: string) => setFolded(prev => {
    const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next;
  });
  const headName = repos.length === 1 ? repos[0] : "Axon";
  const statusLabel = __AXON_DEMO__ ? "Demo data" : connected ? "Live API" : "API unreachable";
  const statusSub = __AXON_DEMO__ ? "fixture graph — API not in use"
    : connected ? "/api/v2 reachable" : "start axon-server, then Reconnect";
  const statusDot = connected && !__AXON_DEMO__ ? "var(--ok)" : "var(--risk)";
  return (
    <aside className={`side ${collapsed ? "collapsed" : ""}`}>
      <div ref={popRef} className="side-head" style={{ position: "relative", padding: "8px 8px 2px", display: "flex", gap: 4, alignItems: "center" }}>
        <button className="wsbtn" style={{ flex: 1, minWidth: 0 }} onClick={() => setPop(!pop)} aria-haspopup="menu" aria-expanded={pop}
          title={collapsed ? headName : undefined}>
          <span className="ws-logo" style={{ background: repoColor(0) }}>A</span>
          <span className="ws-name" style={{ flex: 1, fontWeight: 600, fontSize: 13.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{headName}</span>
          <span className="ws-chev" style={{ color: "var(--tx-3)", display: "inline-flex" }}><ChevronsUpDown size={14} /></span>
        </button>
        {pop && (
          <div className="ws-pop" role="menu">
            <div style={{ fontSize: 10.5, letterSpacing: ".08em", color: "var(--tx-3)", padding: "4px 8px 6px" }}>INDEXED REPOS</div>
            {repos.length === 0 && (
              <div className="dim" style={{ fontSize: 13, padding: "4px 8px 6px" }}>None yet — index a repo below.</div>)}
            {repos.map((r, i) => (
              <button key={r} className="wsrow" onClick={() => { setPop(false); go({ kind: "services" }); }}>
                <span className="ws-logo" style={{ background: repoColor(i) }}>{r[0]?.toUpperCase() ?? "?"}</span>
                <span style={{ flex: 1, fontSize: 13.5 }}>{r}</span>
                <span style={{ fontSize: 11.5, color: "var(--tx-3)" }}>
                  {services.filter((s) => s.repo_id === r).length} svc
                </span>
              </button>
            ))}
            <div style={{ borderTop: "1px solid var(--line-soft)", margin: "7px 0" }} />
            <button className="wsrow" onClick={() => { setPop(false); go({ kind: "services" }); }}><Plus size={15} /><span style={{ fontSize: 13.5 }}>Index a repo</span></button>
            <button className="wsrow" onClick={() => { setPop(false); go({ kind: "onboard" }); }}><FolderGit2 size={15} /><span style={{ fontSize: 13.5 }}>Onboard new repo</span></button>
            <button className="wsrow" onClick={() => { setPop(false); onReconnect(); }}><RefreshCw size={15} /><span style={{ fontSize: 13.5 }}>Reconnect to API</span></button>
          </div>
        )}
      </div>
      <div className="side-scroll">
        <Item collapsed={collapsed} icon={<Bot {...i16} />} label="Ask Axon"
          on={route.kind === "ask"} onClick={() => go({ kind: "ask" })} />
        <div className="sgroup-h">CATALOG</div>
        <Item collapsed={collapsed} icon={<Boxes {...i16} />} label="Services"
          count={services.length ? String(services.length) : undefined}
          on={route.kind === "services" || route.kind === "service"}
          onClick={() => go({ kind: "services" })} />
        <Item collapsed={collapsed} icon={<Waypoints {...i16} />} label="APIs"
          count={deprecated ? String(deprecated) : apis.length ? String(apis.length) : undefined}
          pill={!!deprecated}
          on={route.kind === "apis" || route.kind === "api"}
          onClick={() => go({ kind: "apis" })} />
        <div className="sgroup-h">SERVICES</div>
        <div className="side-projects" role="group" aria-label="Services"
          style={{ maxHeight: "min(34vh, 320px)", overflowY: "auto", overscrollBehavior: "contain", scrollbarWidth: "thin", margin: "0 -2px", padding: "0 2px" }}>
        {services.map(s => {
          const svcApis = apis.filter((a) => a.service_id === s.service_id && a.repo_id === s.repo_id);
          const isOpen = openService === s.service_id;
          const expanded = isOpen && !folded.has(s.service_id);
          const svcDeprecated = svcApis.some((a) => a.props.deprecated === "true");
          return (
          <div key={s.id}>
            <div style={{ display: "flex", alignItems: "center" }}>
              <button className={`sitem ${isOpen ? "on" : ""}`} style={{ flex: 1, minWidth: 0, width: "auto" }}
                onClick={() => go({ kind: "service", id: s.service_id })} title={collapsed ? s.service_id : undefined}>
                <span className="ic"><span style={{ width: 17, height: 17, borderRadius: 5, background: "var(--bg-3)", color: "var(--tx-2)", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 10.5 }}>{s.service_id[0]?.toUpperCase()}</span></span>
                <span className="lbl">{s.service_id}</span>
                <span className="ct"><span className="dot" style={{ background: svcApis.length === 0 ? "var(--off)" : svcDeprecated ? "var(--risk)" : "var(--ok)", width: 6, height: 6 }} /></span>
              </button>
              {isOpen && !collapsed && (
                <button className="proj-fold" onClick={() => toggle(s.service_id)}
                  aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} ${s.service_id} APIs`}
                  title={`${expanded ? "Collapse" : "Expand"} ${s.service_id} APIs`}
                  style={{ flex: "none", width: 24, height: 24, marginLeft: 2, borderRadius: 6, display: "inline-flex", alignItems: "center", justifyContent: "center", color: "var(--tx-3)" }}>
                  <ChevronRight size={14} style={{ transform: expanded ? "rotate(90deg)" : "none", transition: "transform .22s var(--ease)" }} />
                </button>)}
            </div>
            {!collapsed && (
              <div style={{ display: "grid", gridTemplateRows: expanded ? "1fr" : "0fr", transition: "grid-template-rows .26s cubic-bezier(.32,.72,0,1)" }}>
                <div style={{ overflow: "hidden" }}>
                  <div className="subnav" style={{ marginLeft: 10 }} aria-hidden={!expanded || undefined}>
                    {svcApis.slice(0, 6).map(a => (
                      <button key={a.id} tabIndex={expanded ? 0 : -1} className={`sitem ${route.kind === "api" && route.id === a.id ? "on" : ""}`} style={{ fontSize: 13 }}
                        onClick={() => go({ kind: "api", id: a.id })}>
                        <span className="ic" style={{ fontSize: 10 }}>○</span>
                        <span className="lbl mono" style={{ fontSize: 12 }}>{a.props.path}</span>
                        <span className="ct mono" style={{ fontSize: 10.5 }}>{a.props.method}</span>
                      </button>
                    ))}
                    <button className="sitem" tabIndex={expanded ? 0 : -1} style={{ fontSize: 13 }}
                      onClick={() => go({ kind: "apis", service: s.service_id })}>
                      <span className="ic"><Plus size={14} /></span><span className="lbl">All {svcApis.length} APIs</span>
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>);
        })}
        </div>
        <div className="sgroup-h">EXPLORE</div>
        <Item collapsed={collapsed} icon={<ScanSearch {...i16} />} label="Function → APIs"
          on={route.kind === "function"} onClick={() => go({ kind: "function" })} />
        <Item collapsed={collapsed} icon={<GitPullRequest {...i16} />} label="PR impact"
          on={route.kind === "impact"} onClick={() => go({ kind: "impact" })} />
        <Item collapsed={collapsed} icon={<FolderGit2 {...i16} />} label="Onboard repo"
          on={route.kind === "onboard"} onClick={() => go({ kind: "onboard" })} />
        <div style={{ borderTop: "1px solid var(--line-soft)", margin: "14px 4px 6px" }} />
        <Item collapsed={collapsed} icon={<Keyboard {...i16} />} label="Command palette"
          count="⌘K" onClick={onPalette} />
      </div>
      <div style={{ padding: "8px", borderTop: "1px solid var(--line-soft)" }}>
        <div ref={footRef} style={{ position: "relative" }}>
          {footPop && (
            <div className="ws-pop" style={{ top: "auto", bottom: 42, left: 0, width: 264 }} role="menu">
              <div style={{ display: "flex", gap: 11, alignItems: "center", padding: "8px 8px 10px" }}>
                <span className="dot" style={{ background: statusDot, width: 10, height: 10 }} />
                <span>
                  <span style={{ display: "block", fontWeight: 600, fontSize: 13.5 }}>
                    {statusLabel}
                  </span>
                  <span style={{ display: "block", color: "var(--tx-3)", fontSize: 12 }}>
                    {statusSub}
                  </span>
                </span>
              </div>
              <div style={{ fontSize: 10.5, letterSpacing: ".08em", color: "var(--tx-3)", padding: "10px 8px 6px" }}>THEME</div>
              <div style={{ display: "flex", background: "var(--bg-1)", border: "1px solid var(--line-soft)", borderRadius: "var(--r-s)", padding: 3, gap: 3 }} role="group" aria-label="Theme">
                {([["light", Sun, "Light"], ["dark", Moon, "Dark"], ["system", Monitor, "System"], ["colour", Palette, "Colour"]] as const).map(([t, I, label]) => (
                  <button key={t} onClick={() => setTheme(t)} aria-pressed={theme === t} aria-label={label} title={label}
                    style={{ flex: 1, display: "inline-flex", alignItems: "center", justifyContent: "center", padding: "6px 4px", borderRadius: 5,
                      background: theme === t ? "var(--bg-3)" : "transparent", color: theme === t ? "var(--tx)" : "var(--tx-3)" }}>
                    <I size={15} aria-hidden /></button>
                ))}
              </div>
              {theme === "colour" && (
                <div>
                  <div className="swatch-row" role="group" aria-label="Hint colour">
                    {HUE_SWATCHES.map(s => (
                      <button key={s.hue} type="button" className={`swatch ${hue === s.hue ? "on" : ""}`}
                        style={{ background: swatchColor(s.hue) }} title={s.name}
                        aria-label={`${s.name} hint`} aria-pressed={hue === s.hue}
                        onClick={() => setHue(s.hue)} />
                    ))}
                    <label className="swatch-custom" title="Custom colour"
                      style={{ background: swatchColor(hue) }}>
                      <input type="color" value={hueToHex(hue)} aria-label="Custom hint colour"
                        onChange={e => setHue(hexToHue(e.target.value))} />
                    </label>
                  </div>
                  <div className="dim" style={{ fontSize: 11.5, padding: "2px 8px 0" }}>
                    A hint of one colour over your system light/dark. Status colours never change.
                  </div>
                </div>)}
              <div style={{ borderTop: "1px solid var(--line-soft)", margin: "8px 0" }} />
              <button className="wsrow" onClick={() => { setFootPop(false); onReconnect(); }}><RefreshCw size={15} /><span style={{ fontSize: 13.5 }}>Reconnect</span></button>
              <button className="wsrow" onClick={() => { setFootPop(false); onPalette(); }}><Keyboard size={15} /><span style={{ fontSize: 13.5, flex: 1 }}>Keyboard shortcuts</span><span className="chip">⌘K</span></button>
            </div>
          )}
          <button className="sitem" title={collapsed ? statusLabel : undefined}
            onClick={() => setFootPop(!footPop)} aria-haspopup="menu" aria-expanded={footPop}>
            <span className="ic"><span className="dot" style={{ background: statusDot, width: 8, height: 8 }} /></span>
            <span className="lbl">{statusLabel}</span>
            <span className="ct"><ChevronsUpDown size={13} /></span>
          </button>
        </div>
      </div>
    </aside>
  );
}
