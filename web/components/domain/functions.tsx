import { Fragment, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { definitionUrl, groupByName, ownerChipLabel } from "@base/functions";
import { nodeLabel } from "@base/catalog";
import type { FunctionDetail, FunctionOwner, FunctionRow, Route } from "@base/types";

export function OwnerChip({ owner, multiRepo = false }: { owner: FunctionOwner; multiRepo?: boolean }) {
  const tone = ({ shared: "info", client: "accent", unowned: "risk" } as Record<string, string>)[owner.kind];
  return <span className={`chip ${tone ? `tone-${tone}` : ""}`} title={owner.key}>
    {ownerChipLabel(owner, multiRepo)}</span>;
}

export function FunctionTable({ rows, selected, grouped, unreached, multiRepo, onSelect }: {
  rows: FunctionRow[]; selected?: string; grouped: boolean; unreached: boolean;
  multiRepo: boolean; onSelect: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const row = (r: FunctionRow) => <tr key={r.symbol.id} data-symbol={r.symbol.id} tabIndex={0}
    className={`trow ${selected === r.symbol.id ? "fn-selected" : ""}`}
    aria-label={`Inspect ${nodeLabel(r.symbol)}, ${r.owner.label}`} aria-selected={selected === r.symbol.id}
    onClick={() => onSelect(r.symbol.id)} onKeyDown={e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(r.symbol.id); }
    }}>
    <td><div className="fn-cell"><span className="fn-name mono" title={nodeLabel(r.symbol)}>{nodeLabel(r.symbol)}</span>
      <div className="fn-chips"><span className="chip">{r.symbol.props.kind?.toLowerCase() || "function"}</span>
        {r.same_name_count >= 2 && <span className="chip" title="Definitions with this name in distinct owners">{r.same_name_count} definitions</span>}</div></div></td>
    <td><OwnerChip owner={r.owner} multiRepo={multiRepo} /></td>
    <td><span className="fn-package mono dim" title={r.package}>{r.package || "—"}</span></td>
    {!unreached && <><td className="num">{r.api_count}</td><td className="num">{r.service_count}
      {r.service_count >= 2 && <span className="chip tone-info fn-cross" title="Reached by APIs in at least two services">cross-service</span>}</td></>}
    <td className="num">{r.caller_count}</td>
    <td><span className="fn-location mono dim" title={`${r.symbol.file}:${r.symbol.line_start}`}>{r.symbol.file}:{r.symbol.line_start}</span></td>
  </tr>;
  return <div className="fn-table-scroll"><table className="apis fn-table">
    <thead><tr><th>Function</th><th title="Owner declared by axon.yaml: service, shared root, client, or unowned top directory">Defined in</th>
      <th title="Module or package derived from the file path and language">Package</th>
      {!unreached && <><th className="num" title="Endpoints whose handler CALLS closure contains this symbol, at depth ≤ 12">APIs</th>
        <th className="num" title="Distinct repository and service pairs among reached APIs">Services</th></>}
      <th className="num" title="Distinct symbols with a CALLS edge into this symbol">Callers</th><th>Location</th></tr></thead>
    <tbody>{grouped ? groupByName(rows).map(group => {
      const duplicates = Math.max(...group.rows.map(r => r.same_name_count)) >= 2;
      const open = !duplicates || expanded.has(group.name);
      return <Fragment key={group.name}>
        <tr className="fn-group"><th colSpan={unreached ? 5 : 7} scope="rowgroup">
          <button type="button" className="fn-group-button" aria-expanded={open} disabled={!duplicates}
            onClick={() => setExpanded(previous => {
              const next = new Set(previous);
              if (next.has(group.name)) next.delete(group.name); else next.add(group.name);
              return next;
            })}><span aria-hidden>{open ? "▾" : "▸"}</span> {group.name}
            <span className="dim"> · {Math.max(...group.rows.map(r => r.same_name_count))} definitions · {group.rows.length} on this page</span>
          </button>
        </th></tr>{open && group.rows.map(row)}</Fragment>;
    }) : rows.map(row)}</tbody>
  </table></div>;
}

export function FunctionInspector({ detail, multiRepo, used, repo, onClose, openApi, go }: {
  detail: FunctionDetail; multiRepo: boolean; used: string[]; repo?: string; onClose: () => void;
  openApi: (id: string) => void; go: (route: Route) => void;
}) {
  const [tab, setTab] = useState<"apis" | "callers">("apis");
  const [copied, setCopied] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [detail.symbol.id]);
  useEffect(() => {
    const handle = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [onClose]);
  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied("Copied"); }
    catch { setCopied("Copy unavailable"); }
  };
  const definition = definitionUrl(detail.source_path, detail.symbol.file, detail.symbol.line_start);
  const groups = new Map<string, FunctionDetail["apis"]>();
  for (const api of detail.apis) {
    if (repo && api.endpoint.repo_id !== repo) continue;
    const key = `${api.endpoint.repo_id}/${api.endpoint.service_id}`;
    if (used.length && !used.includes(key)) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(api);
  }
  return <aside className="inspector fn-inspector" aria-label={`Function inspector for ${nodeLabel(detail.symbol)}`}>
    <div className="insp-head"><OwnerChip owner={detail.owner} multiRepo={multiRepo} /><span className="spacer" />
      <button className="iconbtn sm" aria-label="Close function inspector" onClick={onClose}><X size={14} aria-hidden /></button></div>
    <h2 ref={heading} tabIndex={-1} className="insp-title mono">{nodeLabel(detail.symbol)}</h2>
    <p className="dim t-sm fn-break">{detail.package}</p>
    <p className="mono dim t-sm fn-break">{detail.symbol.file}:{detail.symbol.line_start}</p>
    <div className="fn-actions"><button className="btn sm" onClick={() => go({ kind: "ask", repo: detail.symbol.repo_id,
      q: `Which APIs include the function ${detail.symbol.props.qualified || detail.symbol.name} (${detail.symbol.id})?` })}>Ask about this function</button>
      <button className="btn sm" onClick={() => void copy(detail.symbol.id)}>Copy symbol id</button>
      {definition && <a className="btn sm" href={definition}>Open definition</a>}
      <button className="btn sm" onClick={() => void copy(`${detail.symbol.file}:${detail.symbol.line_start}`)}>Copy definition location</button>
      <span className="dim t-sm" role="status">{copied}</span></div>
    <div className="insp-tabs" role="tablist" aria-label="Function evidence">
      {(["apis", "callers"] as const).map(t => <button key={t} role="tab" id={`fn-tab-${t}`}
        aria-controls={`fn-panel-${t}`} aria-selected={tab === t} className={`pill ${tab === t ? "on" : ""}`}
        onClick={() => setTab(t)} onKeyDown={e => {
          if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
            const next = t === "apis" ? "callers" : "apis"; setTab(next);
            document.getElementById(`fn-tab-${next}`)?.focus();
          }
        }}>{t === "apis" ? "APIs" : "Direct callers"}</button>)}</div>
    <div className="insp-body" role="tabpanel" id={`fn-panel-${tab}`} aria-labelledby={`fn-tab-${tab}`}>
      {tab === "apis" && <>
        <p className="dim t-sm">Shortest handler → function paths, up to 12 calls. Confidence is the weakest CALLS edge on each path.</p>
        {groups.size === 0 && <p className="dim t-sm">Not reached by any indexed API in this scope.</p>}
        {[...groups].map(([key, apis]) => <section key={key} className="fn-api-group"><h3 className="t-label insp-sub">{multiRepo ? key : apis[0].endpoint.service_id}</h3>
          {apis.map(a => <div key={a.endpoint.id} className="fn-evidence"><button className="attn-row mono" onClick={() => openApi(a.endpoint.id)}>{nodeLabel(a.endpoint)} ↗</button>
            <div className="fn-chips"><span className={`chip ${a.confidence === "AMBIGUOUS" ? "tone-risk" : ""}`}>{a.confidence}</span><span className="dim t-sm">depth {a.depth}</span></div>
            <ol className="fn-path">{a.path.map((node, i) => <li key={`${node.id}-${i}`} className="mono t-sm" title={`${node.file}:${node.line_start}`}>
              {i > 0 && <span aria-hidden>→ </span>}{nodeLabel(node)}<span className="dim fn-path-loc">{node.file}:{node.line_start}</span>
              {i > 0 && a.edges[i - 1] && <span className="dim fn-path-loc">{a.edges[i - 1].confidence} · {a.edges[i - 1].evidence?.rule || "unknown rule"}</span>}</li>)}</ol></div>)}</section>)}
      </>}
      {tab === "callers" && <>
        <p className="dim t-sm">Direct CALLS evidence includes callers outside API closures.</p>
        {detail.callers.length === 0 && <p className="dim t-sm">No direct callers in indexed code.</p>}
        {detail.callers.map((caller, i) => <div key={`${caller.symbol.id}-${i}`} className={`fn-evidence ${caller.symbol.service_id && (caller.symbol.repo_id !== detail.symbol.repo_id || caller.symbol.service_id !== detail.symbol.service_id) ? "fn-other-service" : ""}`}>
          <p className="mono t-sm fn-break">{nodeLabel(caller.symbol)}</p><OwnerChip owner={caller.owner} multiRepo={multiRepo} />
          <p className="mono dim t-sm fn-break">{caller.evidence.file || caller.symbol.file}:{caller.evidence.line || caller.symbol.line_start}</p>
          <div className="fn-chips"><span className="chip">{caller.confidence}</span><span className="dim t-sm">{caller.rule || caller.evidence.rule || "unknown rule"}</span></div>
          {caller.evidence.detail && <p className="dim t-sm">{caller.evidence.detail}</p>}</div>)}
      </>}
    </div>
  </aside>;
}
