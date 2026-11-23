/* Axon base — shared DOMAIN data model (mirrors /api/v1 response shapes).
   App + components speak only this. Look-and-feel types live in ui.ts. */

export type NodeKind = "Repo" | "Service" | "SourceFile" | "Symbol"
  | "ApiEndpoint" | "ApiSpec" | "ApiClient" | "ExternalApi";

export interface GNode {
  id: string;
  kind: NodeKind;
  name: string;
  label?: string;
  repo_id: string;
  service_id: string;
  file: string;
  line_start: number;
  line_end: number;
  props: Record<string, string>;
  api_count?: number;
}

export interface GEdge {
  props?: Record<string, string>;
  confidence?: string;
  evidence?: { file?: string; line?: number; rule?: string; detail?: string };
  from_id: string;
  to_id: string;
  kind: string;
  sites?: CallSite[];
}

export interface CallSite extends Omit<GEdge, "sites"> {
  reason?: string;
  target_repo?: string;
  target_service?: string;
  candidate_nodes?: GNode[];
}

export interface ApiGraph {
  publication_id?: string;
  truncated?: boolean;
  total_symbols?: number;
  total_callers?: number;
  endpoint: GNode | null;
  nodes: GNode[];
  edges: GEdge[];
}

export interface ApiDetail {
  publication_id?: string;
  endpoint: GNode;
  handler: GNode | null;
  handler_source_path?: string;
  handlers?: GNode[];
  handler_edges?: { props: Record<string, string> }[];
  outbound: GNode[];
  outbound_sites?: CallSite[];
  callers: GNode[];
  caller_sites?: CallSite[];
  deprecatable: boolean;
  correction_llm_configured?: boolean;
}

export interface ApiView {
  detail: ApiDetail;
  graph: ApiGraph;
}

export interface CorrectionCall {
  to_service: string; to_method: string; to_path: string; to_repo?: string;
  evidence_file: string; evidence_line: number; evidence_text: string;
}
export interface CorrectionSuppress { to_repo?: string; to_service?: string; from_client?: string; reason: string }
export interface CorrectionBody {
  handler?: string; add_calls?: CorrectionCall[]; suppress?: CorrectionSuppress[]; prompt?: string;
}
export interface CorrectionPreview {
  endpoint: string; service: string;
  diffs: Record<string, string>;
  effects: Record<string, unknown>[];
  warnings: string[];
  draft?: CorrectionBody | null;
  llm_configured?: boolean;
  correction_writable?: boolean;
  applied?: boolean;
  job_id?: string;
}
export interface IndexJobStatus {
  job_id: string; status: string; phase?: string; done?: number; total?: number;
  error?: string; result?: Record<string, unknown> | null;
}

export interface Citation {
  repo_id?: string;
  service_id?: string;
  node_id: string;
  label: string;
  file: string;
  line: number;
}

export interface AnswerRow {
  source: Citation;
  target?: Citation | null;
  via?: Citation | null;
  rule: string;
  confidence?: string | null;
}

export interface AskResponse {
  publication_id?: string;
  answer: string;
  intent: string;
  citations: Citation[];
  evidence: Record<string, unknown>[];
  cypher: string;
  model_used: boolean;
  grounded: boolean;
  summary?: string;
  rows?: AnswerRow[];
  blind_spots?: string[];
  bindings?: Record<string, string>[];
}

export type AskGraphNodeKind = "endpoint" | "external" | "symbol" | "service";
export interface AskGraphNode {
  id: string; kind: AskGraphNodeKind; label: string; sub: string; service: string; repo: string;
  citation: Citation | null;
}
export interface AskGraphEdge {
  from: string; to: string; kind: "call" | "include"; count: number;
  viaLabel: string; rule: string; confidence: string;
}
export interface AskGraphData {
  graphable: boolean; aggregated: boolean;
  nodes: AskGraphNode[]; edges: AskGraphEdge[];
  totals: { apis: number; calls: number; services: number; rows: number };
}

export interface ImpactResult {
  publication_id?: string; indexed_commit?: string;
  affected_endpoint_ids?: string[]; limitations?: string[];
  changed_files: string[];
  changed_symbols: string[];
  affected_apis: string[];
  affected_services: string[];
  markdown?: string;
}

export interface OnboardReport {
  draft_yaml: string;
  services: Record<string, unknown>[];
  ambiguities: string[];
  notes: string[];
  evidence?: Record<string, string[]>;
  written?: boolean;
}

/** Model thinking effort (Arc base/types vocabulary, used by the
 *  copied ModelPicker). Axon's ask engines don't expose effort yet. */
export type Effort = "low" | "medium" | "high" | "max";

export type Route = { repo?: string } & (
  | { kind: "services" }
  | { kind: "service"; id: string }
  | { kind: "apis"; service?: string }
  | { kind: "api"; id: string; service?: string }
  | { kind: "ask"; q?: string }
  | { kind: "function"; q?: string; used?: string[]; defined?: string[]; kindFilter?: string[]; sort?: FunctionSort; fn?: string; hideHandlers?: boolean; unreached?: boolean; group?: boolean; offset?: number }
  | { kind: "impact" }
  | { kind: "onboard" });


export type FunctionSort = "usage" | "apis" | "callers" | "name";
export interface FunctionOwner { key: string; kind: string; id: string; label: string }
export interface Facet { key: string; label: string; count: number }
export interface FunctionUse { key: string; repo_id: string; service_id: string; api_count: number }
export interface FunctionRow {
  symbol: GNode; owner: FunctionOwner; package: string;
  api_count: number; service_count: number; caller_count: number;
  used_in: FunctionUse[]; same_name_count: number; is_handler: boolean; min_depth: number | null;
}
export interface FunctionsPage {
  items: FunctionRow[]; total: number;
  facets: { used_in: Facet[]; defined_in: Facet[]; kind: Facet[] };
}
export interface FunctionDetail {
  source_path?: string;
  symbol: GNode; owner: FunctionOwner; package: string;
  apis: { endpoint: GNode; path: GNode[]; depth: number; confidence: string; edges: GEdge[] }[];
  callers: { symbol: GNode; owner: FunctionOwner; confidence: string;
    evidence: { file?: string; line?: number; rule?: string; detail?: string }; rule?: string }[];
}
export interface FunctionQuery {
  q?: string; repo?: string; used_in?: string[]; defined_in?: string[]; kind?: string[];
  sort?: FunctionSort; hide_handlers?: boolean; unreached?: boolean; limit?: number; offset?: number;
}
