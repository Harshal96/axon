/* Demo-build data source: serves the fixture dataset from demo.ts.
   Only reachable in the demo build (`vite --mode demo`), where the
   @client alias points here instead of live-client.ts. The main build
   never imports this module, so no demo data ships in it.
   Signatures are pinned to the live client with type-only imports. */
import type * as Live from "./live-client.ts";
import { DEMO_APIS, DEMO_SERVICES, DEMO_SYMBOLS, demoGraph } from "./demo.ts";

export type AskEngine = Live.AskEngine;

export const probe: typeof Live.probe = async () => true;

export const fetchServices: typeof Live.fetchServices = async () => DEMO_SERVICES;

export const fetchApis: typeof Live.fetchApis = async (service = "", q = "", repo = "") =>
  DEMO_APIS.filter((a) =>
    (!service || a.service_id === service)
    && (!repo || a.repo_id === repo)
    && (!q || (a.props.path + " " + (a.props.operation_id ?? "") + " " + a.service_id)
      .toLowerCase().includes(q.toLowerCase())));

export const fetchGraph: typeof Live.fetchGraph = async (endpointId) => demoGraph(endpointId);

export const fetchDetail: typeof Live.fetchDetail = async (endpointId) => {
  const g = demoGraph(endpointId);
  const ep = g.endpoint ?? DEMO_APIS[0];
  const inbound = new Set(
    g.edges.filter((e) => e.kind === "CALLS_API" && e.to_id === ep.id).map((e) => e.from_id));
  const outboundIds = new Set(
    g.edges.filter((e) => e.kind === "CALLS_API" && e.to_id !== ep.id).map((e) => e.to_id));
  return {
    endpoint: ep,
    handler: g.nodes.find((n) => n.kind === "Symbol"
      && n.props.qualified === ep.props.operation_id) ?? null,
    outbound: g.nodes.filter((n) => outboundIds.has(n.id)),
    callers: g.nodes.filter((n) => inbound.has(n.id)),
    deprecatable: inbound.size === 0,
  };
};

export const fetchFunctionApis: typeof Live.fetchFunctionApis = async (q) => {
  const hit = DEMO_SYMBOLS
    .filter((s) => s.name.toLowerCase().includes(q.toLowerCase()));
  return hit.map((symbol) => ({
    symbol,
    apis: DEMO_APIS.filter((a) => a.props.operation_id === symbol.name),
  }));
};

export const ask: typeof Live.ask = async () => ({
  answer: "Demo build: start the Axon API (uv run axon-server), index a repo, and use the main build to ask real questions. Deterministic questions need no model.",
  intent: "demo", citations: [], evidence: [], cypher: "",
  model_used: false, grounded: true,
});

const needsLiveApi = (what: string) => async (): Promise<never> => {
  throw new Error(`${what} needs the live Axon API — this is the demo build.`);
};

export const indexRepo: typeof Live.indexRepo = needsLiveApi("Indexing");
export const impact: typeof Live.impact = needsLiveApi("PR impact");
export const onboard: typeof Live.onboard = needsLiveApi("Onboarding");
