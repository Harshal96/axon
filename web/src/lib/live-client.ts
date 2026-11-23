/* Live /api/v1 client — the only data source in the main build.
   The demo build swaps this module for demo-client.ts via the @client
   alias in vite.config.ts; this file never imports demo data. */
import type {
  ApiGraph, ApiView, AskResponse, CorrectionBody, CorrectionPreview, GNode,
  ImpactResult, IndexJobStatus, OnboardReport,
  FunctionDetail, FunctionQuery, FunctionsPage,
} from "../../base/types.ts";

import { functionQueryParams } from "../../base/functions.ts";

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const error = await resp.json().catch(() => null);
    throw new ApiRequestError(resp.status,
      typeof error?.detail === "string" ? error.detail : `${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as T;
}

/** True when /api/v1 answers. Drives the connection status chip only;
    data fetches fail honestly when the API is down. */
export async function probe(): Promise<boolean> {
  try {
    await req("/api/v1/services");
    return true;
  } catch {
    return false;
  }
}

export async function fetchServices(): Promise<GNode[]> {
  return req<GNode[]>("/api/v1/services");
}

export async function fetchApis(service = "", q = "", repo = ""): Promise<GNode[]> {
  const p = new URLSearchParams();
  if (service) p.set("service", service);
  if (repo) p.set("repo", repo);
  if (q) p.set("q", q);
  return req<GNode[]>(`/api/v1/apis?${p.toString()}`);
}

export async function fetchGraph(endpointId: string): Promise<ApiGraph> {
  const params = new URLSearchParams({ endpoint_id: endpointId });
  return req<ApiGraph>(`/api/v1/api-graph?${params}`);
}

export async function fetchView(endpointId: string): Promise<ApiView> {
  const params = new URLSearchParams({ endpoint_id: endpointId });
  return req<ApiView>(`/api/v1/api-view?${params}`);
}

export async function recheckApi(endpointId: string): Promise<{ job_id: string }> {
  return req(`/api/v1/apis/${encodeURIComponent(endpointId)}/recheck`, { method: "POST" });
}

export async function previewCorrection(
  endpointId: string, body: CorrectionBody,
): Promise<CorrectionPreview> {
  return req(`/api/v1/apis/${encodeURIComponent(endpointId)}/corrections/preview`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function applyCorrection(
  endpointId: string, body: CorrectionBody,
): Promise<CorrectionPreview> {
  return req(`/api/v1/apis/${encodeURIComponent(endpointId)}/corrections/apply`, {
    method: "POST", body: JSON.stringify(body),
  });
}

export async function fetchIndexJob(jobId: string): Promise<IndexJobStatus> {
  return req(`/api/v1/index/jobs/${encodeURIComponent(jobId)}`);
}

export async function fetchFunctionApis(
  q: string,
): Promise<{ symbol: GNode; apis: GNode[] }[]> {
  return req(`/api/v1/functions/apis?q=${encodeURIComponent(q)}`);
}

export type AskEngine = "auto" | "deterministic" | "model";

export async function ask(
  question: string, engine: AskEngine = "auto",
): Promise<AskResponse> {
  return req<AskResponse>("/api/v1/ask", {
    method: "POST", body: JSON.stringify({ question, engine }),
  });
}

export async function indexRepo(repoPath: string): Promise<{ repo: string; nodes: number; edges: number; unresolved: string[] }> {
  return req("/api/v1/index", {
    method: "POST", body: JSON.stringify({ repo_path: repoPath }),
  });
}

export async function impact(repoPath: string, changedFiles: string[]): Promise<ImpactResult> {
  return req<ImpactResult>("/api/v1/impact", {
    method: "POST",
    body: JSON.stringify({ repo_path: repoPath, changed_files: changedFiles }),
  });
}

export async function onboard(repoPath: string): Promise<OnboardReport> {
  return req<OnboardReport>("/api/v1/onboard/scan", {
    method: "POST", body: JSON.stringify({ repo_path: repoPath }),
  });
}

export async function fetchFunctions(query: FunctionQuery = {}): Promise<FunctionsPage> {
  return req(`/api/v1/functions?${functionQueryParams(query)}`);
}
export async function fetchFunctionDetail(id: string): Promise<FunctionDetail> {
  return req(`/api/v1/functions/${encodeURIComponent(id)}`);
}
