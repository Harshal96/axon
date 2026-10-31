/* NodeInspector — the right sidebar that opens when a graph node is
 *  clicked. Two tabs today (Info, Interact); this is the panel future
 *  per-node surfaces (edit, runs, comments…) should plug into as
 *  additional tabs — add the tab here, not a new floating UI.
 *
 *  Everything shown is derived from the node + the ego graph it was
 *  clicked in; actions route through the app's real navigation. */
import { useState } from "react";
import {
  ArrowDownLeft, ArrowUpRight, Bot, Boxes, Check, Copy, Crosshair,
  ScanSearch, X,
} from "lucide-react";
import type { ApiGraph, GNode, Route } from "../../base/types";
import { nodeLabel } from "../../base/catalog";
import { egoEdges } from "../../base/layout";
import { MethodChip, SourceChip } from "./catalog";

type Tab = "info" | "interact";

function Row({ k, children, mono }: {
  k: string; children: React.ReactNode; mono?: boolean;
}) {
  return (
    <div className="insp-row">
      <span className="insp-k">{k}</span>
      <span className={`insp-v ${mono ? "mono" : ""}`}>{children}</span>
    </div>
  );
}

function CopyBtn({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" className="iconbtn sm" aria-label={label} title={label}
      onClick={() => {
        void navigator.clipboard?.writeText(text).catch(() => undefined);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      }}>
      {copied ? <Check size={12} aria-hidden /> : <Copy size={12} aria-hidden />}
    </button>
  );
}

export function NodeInspector({ node, graph, centerId, onClose, onSelect, openApi, go }: {
  node: GNode;
  graph: ApiGraph;
  centerId: string;
  onClose: () => void;
  onSelect: (id: string) => void;
  openApi: (id: string) => void;
  go: (r: Route) => void;
}) {
  const [tab, setTab] = useState<Tab>("info");
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  /* Same cleaned edge set the canvas draws (no handler self-edges). */
  const edges = egoEdges(graph.edges, centerId);
  const incoming = edges.filter((e) => e.to_id === node.id);
  const outgoing = edges.filter((e) => e.from_id === node.id);
  const isCenter = node.id === centerId;
  const isApi = node.kind === "ApiEndpoint";
  const ask = (q: string) => go({ kind: "ask", q });

  const actions: { icon: React.ReactNode; label: string; run: () => void; disabled?: boolean }[] = [];
  if (isApi) {
    actions.push({
      icon: <Crosshair size={14} aria-hidden />, label: "Centre flow on this API",
      run: () => openApi(node.id), disabled: isCenter,
    });
    actions.push({
      icon: <Bot size={14} aria-hidden />,
      label: `Ask who calls ${node.props.path ?? "it"}`,
      run: () => ask(`Who calls ${node.props.method ?? ""} ${node.props.path ?? ""}?`),
    });
    actions.push({
      icon: <Bot size={14} aria-hidden />,
      label: "Ask what this API calls",
      run: () => ask(`What services does ${node.props.method ?? ""} ${node.props.path ?? ""} call?`),
    });
    actions.push({
      icon: <Bot size={14} aria-hidden />,
      label: "Ask if it can be deprecated",
      run: () => ask(`Can ${node.props.method ?? ""} ${node.props.path ?? ""} be deprecated?`),
    });
  }
  if (node.kind === "Symbol") {
    actions.push({
      icon: <ScanSearch size={14} aria-hidden />, label: "Find APIs including this function",
      run: () => go({ kind: "function", q: node.name }),
    });
    actions.push({
      icon: <Bot size={14} aria-hidden />, label: "Ask which APIs include it",
      run: () => ask(`Which APIs is the function ${node.name} a part of?`),
    });
  }
  if (node.kind === "Service") {
    actions.push({
      icon: <Boxes size={14} aria-hidden />, label: `Open ${node.service_id}`,
      run: () => go({ kind: "service", id: node.service_id, repo: node.repo_id }),
    });
  } else if (node.service_id) {
    actions.push({
      icon: <Boxes size={14} aria-hidden />, label: `Open service ${node.service_id}`,
      run: () => go({ kind: "service", id: node.service_id, repo: node.repo_id }),
    });
  }

  return (
    <aside className="inspector" aria-label={`Inspector for ${nodeLabel(node)}`}>
      <div className="insp-head">
        <span className="chip tone-accent">{node.kind}</span>
        {isApi && <MethodChip method={node.props.method ?? ""} />}
        {isCenter && <span className="chip">centred API</span>}
        <span className="spacer" />
        <button type="button" className="iconbtn sm" aria-label="Close inspector"
          title="Close (Esc)" onClick={onClose}>
          <X size={14} aria-hidden />
        </button>
      </div>
      <h2 className="insp-title mono">{nodeLabel(node)}</h2>
      <div className="insp-tabs" role="tablist" aria-label="Inspector tabs">
        {(["info", "interact"] as const).map((t) => (
          <button key={t} role="tab" aria-selected={tab === t}
            className={`pill ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {t === "info" ? "Info" : "Interact"}
          </button>))}
      </div>
      <div className="insp-body">
        {tab === "info" && (<>
          {isApi && <>
            <Row k="Endpoint" mono>{node.props.method} {node.props.path}</Row>
            <Row k="Handler" mono>{node.props.operation_id || "—"}</Row>
            <Row k="Source"><SourceChip api={node} /></Row>
            {node.props.deprecated === "true" && (
              <Row k="Status"><span className="chip tone-off">deprecated in spec</span></Row>)}
          </>}
          {node.kind === "Symbol" && (
            <Row k="Symbol" mono>{node.props.qualified || node.name}</Row>)}
          {node.kind === "ExternalApi" && <>
            <Row k="Host" mono>{node.props.host || "—"}</Row>
            <Row k="URL" mono>{node.props.path || "—"}</Row>
          </>}
          <Row k="Service">{node.service_id || "external"}</Row>
          <Row k="Repo">{node.repo_id || "—"}</Row>
          {node.file && <Row k="Location" mono>
            <span className="insp-loc">{node.file}:{node.line_start}
              <CopyBtn text={`${node.file}:${node.line_start}`} label="Copy location" />
            </span></Row>}
          {node.props.framework && <Row k="Framework">{node.props.framework}</Row>}
          {node.props.language && <Row k="Language">{node.props.language}</Row>}
          <Row k="Node ID" mono>
            <span className="insp-loc">
              <span className="insp-id">{node.id}</span>
              <CopyBtn text={node.id} label="Copy node ID" />
            </span></Row>

          <h3 className="t-label insp-sub">Connections in this flow</h3>
          {incoming.length === 0 && outgoing.length === 0 && (
            <p className="dim t-sm">No edges in this graph.</p>)}
          {incoming.map((e, i) => {
            const other = byId.get(e.from_id);
            if (!other) return null;
            return (
              <button key={`in-${i}`} className="attn-row" onClick={() => onSelect(other.id)}>
                <ArrowDownLeft size={13} aria-hidden />
                <span className="grow">
                  <span className="mono t-sm">{nodeLabel(other)}</span>
                  <span className="dim t-sm"> · {e.kind.toLowerCase()}</span>
                </span>
              </button>);
          })}
          {outgoing.map((e, i) => {
            const other = byId.get(e.to_id);
            if (!other) return null;
            return (
              <button key={`out-${i}`} className="attn-row" onClick={() => onSelect(other.id)}>
                <ArrowUpRight size={13} aria-hidden />
                <span className="grow">
                  <span className="mono t-sm">{nodeLabel(other)}</span>
                  <span className="dim t-sm"> · {e.kind.toLowerCase()}</span>
                </span>
              </button>);
          })}
        </>)}
        {tab === "interact" && (<>
          <div className="stack">
            {actions.map((a) => (
              <button key={a.label} className="btn insp-action" disabled={a.disabled}
                onClick={a.run}>
                {a.icon}<span className="grow">{a.label}</span>
              </button>))}
            {actions.length === 0 && (
              <p className="dim t-sm">No actions for this node yet.</p>)}
          </div>
          <p className="dim t-sm insp-note">
            Per-node actions live in this tab. Future node surfaces
            (runs, edits, comments) plug in here.
          </p>
        </>)}
      </div>
    </aside>
  );
}
