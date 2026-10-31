import { test } from "node:test";
import assert from "node:assert/strict";
import { parseRoute, routeUrl, resolveRoute } from "../base/routing.ts";

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
