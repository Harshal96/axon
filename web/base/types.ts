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
  evidence?: { rule?: string; detail?: string };
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

export interface AskResponse {
  answer: string;
  intent: string;
  citations: Citation[];
  evidence: Record<string, unknown>[];
  cypher: string;
  model_used: boolean;
  grounded: boolean;
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
  | { kind: "function"; q?: string }
  | { kind: "impact" }
  | { kind: "onboard" });
