/* Axon base — shared DOMAIN data model (mirrors /api/v2 response shapes).
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
  confidence?: string;
  evidence?: { file?: string; line?: number; rule?: string; detail?: string };
  from_id: string;
  to_id: string;
  kind: string;
}

export interface ApiGraph {
  truncated?: boolean;
  total_symbols?: number;
  total_callers?: number;
  endpoint: GNode | null;
  nodes: GNode[];
  edges: GEdge[];
}

export interface ApiDetail {
  endpoint: GNode;
  handler: GNode | null;
  handler_edges?: { props: Record<string, string> }[];
  outbound: GNode[];
  callers: GNode[];
  deprecatable: boolean;
}

export interface Citation {
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

export interface ImpactResult {
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
