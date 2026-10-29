/* Fixture dataset for the demo build only (`vite --mode demo`, via
 * demo-client.ts) and for node --test graph-library tests. The main
 * build never imports this file, so none of this data ships in it.
 * Relative imports only, so node --test can load this file directly. */
import type { ApiGraph, GNode } from "../../base/types.ts";

const R = "fixture-monorepo";

function svc(id: string, apiCount: number, framework = "fastapi"): GNode {
  return {
    id: `service::${R}::${id}`, kind: "Service", name: id, label: id,
    repo_id: R, service_id: id, file: "", line_start: 0, line_end: 0,
    props: { language: "python", framework }, api_count: apiCount,
  };
}

function ep(service: string, method: string, path: string, op: string,
            file: string, extra: Record<string, string> = {}): GNode {
  return {
    id: `endpoint::${R}::${service}::${method}:${path}`, kind: "ApiEndpoint",
    name: `${method} ${path}`, label: `${method} ${path}`,
    repo_id: R, service_id: service, file, line_start: 12, line_end: 18,
    props: { method, path, operation_id: op, source: "both", framework: "fastapi", ...extra },
  };
}

function sym(service: string, file: string, qualified: string): GNode {
  const name = qualified.split(".").pop() ?? qualified;
  return {
    id: `symbol::${R}::${file}::${qualified}`, kind: "Symbol", name,
    label: qualified, repo_id: R, service_id: service, file,
    line_start: 5, line_end: 9,
    props: { qualified, kind: "FUNCTION", language: "python" },
  };
}

export const DEMO_EXTERNAL: GNode = {
  id: "external::POST:https://api.stripe.com/v1/charges", kind: "ExternalApi",
  name: "POST api.stripe.com/v1/charges", label: "POST api.stripe.com/v1/charges",
  repo_id: "", service_id: "", file: "", line_start: 0, line_end: 0,
  props: { method: "POST", path: "https://api.stripe.com/v1/charges", host: "api.stripe.com" },
};

export const DEMO_SERVICES: GNode[] = [
  svc("checkout", 2), svc("payment", 3), svc("inventory", 2),
];

export const DEMO_APIS: GNode[] = [
  ep("checkout", "POST", "/checkout", "place_order", "services/checkout/app.py"),
  ep("checkout", "GET", "/checkout/{order_id}/status", "order_status", "services/checkout/app.py"),
  ep("payment", "POST", "/v1/charges", "create_charge", "services/payment/app.py"),
  ep("payment", "GET", "/v1/charges/{charge_id}", "get_charge", "services/payment/app.py"),
  ep("payment", "GET", "/v1/legacy", "legacy_ping", "services/payment/openapi.yaml",
     { source: "spec", deprecated: "true" }),
  ep("inventory", "GET", "/v1/stock/{sku}", "get_stock", "services/inventory/app.py"),
  ep("inventory", "POST", "/v1/reservations", "reserve_stock", "services/inventory/app.py"),
];

export const DEMO_SYMBOLS: GNode[] = [
  sym("checkout", "services/checkout/app.py", "place_order"),
  sym("checkout", "services/checkout/app.py", "price_cart"),
  sym("checkout", "services/checkout/app.py", "order_status"),
  sym("payment", "services/payment/app.py", "create_charge"),
  sym("payment", "services/payment/app.py", "validate_charge"),
  sym("payment", "services/payment/app.py", "ChargeStore.save"),
  sym("payment", "services/payment/app.py", "get_charge"),
  sym("inventory", "services/inventory/app.py", "get_stock"),
  sym("inventory", "services/inventory/app.py", "reserve_stock"),
  sym("checkout", "clients/payment/payment_client.py", "PaymentClient.create_charge"),
  sym("checkout", "clients/payment/payment_client.py", "PaymentClient.get_charge"),
  sym("checkout", "clients/inventory/inventory_client.py", "InventoryClient.reserve"),
  sym("payment", "clients/stripe/stripe_client.py", "StripeClient.charge"),
];

/** Demo call wiring, in call direction: from-endpoint's handler code
 *  (via an optional client symbol) calls the to-endpoint/external. */
interface DemoCall { fromPath: string; fromMethod: string; via?: string; toId: string }
const apiId = (method: string, path: string): string =>
  DEMO_APIS.find((a) => a.props.method === method && a.props.path === path)?.id ?? "";
export const DEMO_CALLS: DemoCall[] = [
  { fromPath: "/checkout", fromMethod: "POST", via: "PaymentClient.create_charge",
    toId: apiId("POST", "/v1/charges") },
  { fromPath: "/checkout", fromMethod: "POST", via: "InventoryClient.reserve",
    toId: apiId("POST", "/v1/reservations") },
  { fromPath: "/checkout/{order_id}/status", fromMethod: "GET", via: "PaymentClient.get_charge",
    toId: apiId("GET", "/v1/charges/{charge_id}") },
  { fromPath: "/v1/charges", fromMethod: "POST", via: "StripeClient.charge",
    toId: DEMO_EXTERNAL.id },
];

const ALL: GNode[] = [...DEMO_SERVICES, ...DEMO_APIS, ...DEMO_SYMBOLS, DEMO_EXTERNAL];
const byId = (id: string) => ALL.find((n) => n.id === id);
const handlerOf = (api: GNode) => DEMO_SYMBOLS.find(
  (s) => s.props.qualified === api.props.operation_id && s.service_id === api.service_id);

export function demoGraph(centerId: string): ApiGraph {
  const center = ALL.find((n) => n.id === centerId) ?? DEMO_APIS[0];
  const nodes = new Map<string, GNode>([[center.id, center]]);
  const edges: { from_id: string; to_id: string; kind: string }[] = [];
  const link = (a: string, b: string, kind: string) => edges.push({ from_id: a, to_id: b, kind });
  const service = DEMO_SERVICES.find((s) => s.service_id === center.service_id);
  if (service) { nodes.set(service.id, service); link(service.id, center.id, "EXPOSES"); }
  const handler = handlerOf(center);
  if (handler) { nodes.set(handler.id, handler); link(handler.id, center.id, "HANDLES"); }

  for (const call of DEMO_CALLS) {
    const target = byId(call.toId);
    if (!target) continue;
    const fromApi = DEMO_APIS.find(
      (a) => a.props.path === call.fromPath && a.props.method === call.fromMethod);
    if (!fromApi) continue;
    const via = call.via
      ? DEMO_SYMBOLS.find((s) => s.props.qualified === call.via) : undefined;
    if (fromApi.id === center.id && handler) {
      // Outbound from this API: handler -> client -> target.
      nodes.set(target.id, target);
      if (via) {
        nodes.set(via.id, via);
        link(handler.id, via.id, "CALLS");
        link(via.id, target.id, "CALLS_API");
      } else link(handler.id, target.id, "CALLS_API");
    }
    if (target.id === center.id) {
      // Inbound to this API: owner API -> its handler -> this API.
      const ownerHandler = handlerOf(fromApi);
      nodes.set(fromApi.id, fromApi);
      if (ownerHandler) {
        nodes.set(ownerHandler.id, ownerHandler);
        link(fromApi.id, ownerHandler.id, "HANDLES_CHAIN");
        link(ownerHandler.id, center.id, "CALLS_API");
      }
    }
  }
  return { endpoint: center, nodes: [...nodes.values()], edges };
}
