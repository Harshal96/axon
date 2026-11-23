import { test } from "node:test";
import assert from "node:assert/strict";
import { parseRoute, routeUrl, resolveRoute, citationRoute, functionRoute } from "../base/routing.ts";

test("repository service and API deep links round trip with reserved characters", () => {
  const route = {kind:"api", repo:"sample / repo", service:"items & tools", id:"endpoint::sample / repo::items & tools::GET:/items/{id}?x=1"};
  const url = new URL(routeUrl(route), "http://localhost");
  assert.equal(url.pathname, "/repos/sample%20%2F%20repo");
  assert.equal(url.searchParams.get("service"), route.service);
  assert.equal(url.searchParams.get("api"), route.id);
  assert.deepEqual(parseRoute(url), route);
});
test("catalog and service links distinguish service detail from filtered catalog", () => {
  for (const route of [
    {kind:"apis"}, {kind:"apis",repo:"sample"},
    {kind:"service",repo:"sample",id:"items"},
    {kind:"apis",repo:"sample",service:"items"},
    {kind:"ask",repo:"sample",q:"Who calls GET /items?"},
    {kind:"services",repo:"sample"}, {kind:"impact"}, {kind:"onboard"},
    {kind:"function", q:"worker.execute"},
  ]) assert.deepEqual(parseRoute(new URL(routeUrl(route),"http://localhost")),route);
});
test("malformed repository escapes fail safely and empty selections become catalog", () => {
  assert.deepEqual(parseRoute(new URL("http://localhost/repos/%ZZ?service=")),{kind:"apis"});
  assert.deepEqual(parseRoute(new URL("http://localhost/repos/sample?api=&service=")),{kind:"apis",repo:"sample"});
});
test("cross repository API navigation follows endpoint ownership", () => {
  const endpoints = [{id:"remote-api",repo_id:"other",service_id:"items"}];
  assert.deepEqual(resolveRoute({kind:"api",id:"remote-api"},{kind:"apis",repo:"sample"},[],endpoints),
    {kind:"api",id:"remote-api",repo:"other",service:"items"});
});
test("same service names select explicit repository and never guess ambiguous ownership", () => {
  const services = [{service_id:"items",repo_id:"one"},{service_id:"items",repo_id:"two"}];
  assert.deepEqual(resolveRoute({kind:"service",id:"items",repo:"two"},{kind:"apis",repo:"one"},services,[]),
    {kind:"service",id:"items",repo:"two"});
  assert.deepEqual(resolveRoute({kind:"service",id:"items"},{kind:"apis"},services,[]),
    {kind:"service",id:"items"});
});

test("repository selection can clear scope and service filtering reaches the live API", async () => {
  assert.equal(routeUrl(resolveRoute({kind:"apis",repo:""},{kind:"apis",repo:"sample"},[],[])), "/");
  const {fetchApis} = await import("../src/lib/live-client.ts");
  const original = globalThis.fetch;
  let requested;
  globalThis.fetch = async (url) => {
    requested = new URL(url,"http://localhost");
    return {ok:true,json:async()=>[]};
  };
  try { await fetchApis("items", "lookup & list", "other repo"); }
  finally { globalThis.fetch = original; }
  assert.equal(requested.searchParams.get("repo"),"other repo");
  assert.equal(requested.searchParams.get("service"),"items");
  assert.equal(requested.searchParams.get("q"),"lookup & list");
});

test('foreign endpoint opens before catalog load and qualified service citations round trip', () => {
  assert.deepEqual(resolveRoute({kind:'api',id:'endpoint::finance::ledger::GET:/entries'},
    {kind:'apis',repo:'orders'},[],[]), {kind:'api',id:'endpoint::finance::ledger::GET:/entries',repo:'finance',service:'ledger'});
  const citation = {node_id:'service::finance/team::ledger',label:'finance/team::ledger',file:'',line:0};
  const route = citationRoute(citation);
  assert.deepEqual(route,{kind:'service',repo:'finance/team',id:'ledger'});
  assert.deepEqual(parseRoute(new URL(routeUrl(route),'http://localhost')),route);
});

test('correction preview and apply preserve remote target qualifiers', async () => {
  const {previewCorrection,applyCorrection} = await import('../src/lib/live-client.ts');
  const original = globalThis.fetch;
  const bodies = [];
  const draft = {add_calls:[{to_repo:'finance',to_service:'ledger',to_method:'GET',to_path:'/entries',
    evidence_file:'src/app.py',evidence_line:7,evidence_text:'requests.get(url)'}],
    suppress:[{to_repo:'third',to_service:'ledger',reason:'wrong target'}]};
  globalThis.fetch = async (_,options) => {
    bodies.push(JSON.parse(options.body));
    return {ok:true,json:async()=>({draft})};
  };
  try {
    const preview = await previewCorrection('endpoint::orders::orders::GET:/run',draft);
    await applyCorrection('endpoint::orders::orders::GET:/run',preview.draft);
  } finally { globalThis.fetch = original; }
  assert.deepEqual(bodies,[draft,draft]);
});

test('unowned external citation fallback does not manufacture a repository scope', () => {
  assert.deepEqual(citationRoute({node_id:'external::GET:/missing',label:'GET /missing',file:'',line:0}),
    {kind:'function',q:'GET /missing'});
});

test("opening a foreign caller keeps its exact symbol, repo and handler visibility", () => {
  const caller = { id: "symbol::orders::checkout::app.start", repo_id: "orders" };
  const route = resolveRoute(functionRoute(caller), {kind:"api",repo:"finance",id:"target"}, [], []);
  assert.deepEqual(parseRoute(new URL(routeUrl(route), "http://localhost")), {
    kind: "function", repo: "orders", fn: caller.id, hideHandlers: false,
  });
});

test("API view uses a single read and cleanly reports a removed endpoint", async () => {
  const {fetchView, ApiRequestError} = await import("../src/lib/live-client.ts");
  const endpointId = "endpoint::finance::ledger::GET:/v1/graph";
  const original = globalThis.fetch;
  const visited = [];
  globalThis.fetch = async (url) => {
    visited.push(new URL(url, "http://localhost"));
    return {ok:false,status:404,json:async()=>({detail:"endpoint not found"})};
  };
  try {
    await assert.rejects(fetchView(endpointId), e => e instanceof ApiRequestError && e.status === 404);
  } finally { globalThis.fetch = original; }
  assert.equal(visited.length, 1);
  assert.equal(visited[0].pathname, "/api/v1/api-view");
  assert.equal(visited[0].searchParams.get("endpoint_id"), endpointId);
});
