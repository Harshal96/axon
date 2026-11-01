import { test } from "node:test";
import assert from "node:assert/strict";
import { groupByName, ownerChipLabel, definitionUrl, facetSummary, functionQueryParams } from "../base/functions.ts";
import { parseRoute, routeUrl, repositoryRoute } from "../base/routing.ts";
import { demoFunctions, demoFunctionDetail } from "../src/lib/demo-functions.ts";

test("function filters, selection and display controls round trip through shareable URLs", () => {
  const route = { kind: "function", repo: "sample / repo", q: "same & name", used: ["sample/alpha", "sample/beta"],
    defined: ["sample/shared/common/path"], kindFilter: ["function", "method"], sort: "callers", fn: "symbol::sample::file::name",
    hideHandlers: false, unreached: true, group: true, offset: 50 };
  assert.deepEqual(parseRoute(new URL(routeUrl(route), "http://localhost")), route);
  const invalid = parseRoute(new URL("http://localhost/?view=function&sort=bad&offset=-10"));
  assert.deepEqual(invalid, { kind: "function" });
});

test("query parameters retain repeated facets and explicit false options", () => {
  const p = functionQueryParams({ q: "name & text", used_in: ["one/alpha", "two/alpha"], defined_in: ["one/shared/common"],
    hide_handlers: false, unreached: true, offset: 0 });
  assert.deepEqual(p.getAll("used_in"), ["one/alpha", "two/alpha"]);
  assert.equal(p.get("q"), "name & text");
  assert.equal(p.get("hide_handlers"), "false");
  assert.equal(p.get("offset"), "0");
});

test("owner labels preserve nested roots and include repo only when requested", () => {
  const owner = { key: "sample/shared/common/path", kind: "shared", id: "common/path", label: "shared · common/path" };
  assert.equal(ownerChipLabel(owner, false), "shared · common/path");
  assert.equal(ownerChipLabel(owner, true), "sample · shared · common/path");
  assert.equal(facetSummary(["known", "missing"], [{ key: "known", label: "Alpha", count: 2 }]), "Alpha, missing");
});

test("grouping preserves ranked group order and all definitions", () => {
  const rows = [{ symbol: { name: "b" } }, { symbol: { name: "a" } }, { symbol: { name: "b" } }];
  assert.deepEqual(groupByName(rows).map(g => [g.name, g.rows.length]), [["b", 2], ["a", 1]]);
});

test("demo catalog shows evidence-backed shared reach and separate definitions", () => {
  const page = demoFunctions({ hide_handlers: true });
  assert.ok(page.items.length > 0);
  assert.ok(page.items.every(row => !row.is_handler));
  const helper = page.items.find(row => row.symbol.name === "clean_text");
  assert.equal(helper.owner.kind, "shared");
  assert.equal(helper.package, "common.text");
  assert.ok(helper.service_count >= 2);
  const detail = demoFunctionDetail(helper.symbol.id);
  assert.equal(detail.apis.length, helper.api_count);
  assert.ok(detail.apis.every(api => api.path.at(-1).id === helper.symbol.id));
  assert.ok(detail.apis.every(api => api.path.length === api.depth + 1));
  assert.ok(detail.callers.length >= 2);
  const names = page.items.filter(row => row.symbol.name === "format_value");
  assert.equal(names.length, 2);
  assert.ok(names.every(row => row.same_name_count === 2));
});

test("demo service scope recalculates reach, facets exclude their own filter, and paging is stable", () => {
  const all = demoFunctions({ hide_handlers: true });
  const shared = all.items.find(row => row.owner.kind === "shared");
  const key = shared.used_in[0].key;
  const scoped = demoFunctions({ hide_handlers: true, used_in: [key] });
  assert.ok(scoped.items.every(row => row.api_count > 0 && row.service_count === 1));
  assert.ok(scoped.facets.used_in.length > 1);
  assert.ok(demoFunctions({ repo: "absent" }).total === 0);
  const first = demoFunctions({ hide_handlers: true, limit: 1 });
  const second = demoFunctions({ hide_handlers: true, limit: 1, offset: 1 });
  assert.equal(first.total, all.total);
  assert.notEqual(first.items[0].symbol.id, second.items[0].symbol.id);
});

test("live function client uses the agreed catalog and encoded detail routes", async () => {
  const { fetchFunctions, fetchFunctionDetail } = await import("../src/lib/live-client.ts");
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async url => { requests.push(url); return { ok: true, json: async () => ({}) }; };
  try {
    await fetchFunctions({ repo: "sample", used_in: ["sample/alpha", "sample/beta"], hide_handlers: false });
    await fetchFunctionDetail("symbol::sample::common/file.py::name");
  } finally { globalThis.fetch = original; }
  const catalog = new URL(requests[0], "http://localhost");
  assert.equal(catalog.pathname, "/api/v2/functions");
  assert.deepEqual(catalog.searchParams.getAll("used_in"), ["sample/alpha", "sample/beta"]);
  assert.equal(requests[1], "/api/v2/functions/symbol%3A%3Asample%3A%3Acommon%2Ffile.py%3A%3Aname");
});

test("definition links only use recorded roots and encode reserved path characters", () => {
  assert.equal(definitionUrl(undefined, "common/file.py", 4), undefined);
  assert.equal(definitionUrl("/work/sample", "../secret", 4), undefined);
  assert.equal(definitionUrl("/work/sample", "/other/file.py", 4), undefined);
  assert.equal(definitionUrl("/work/sample #1", "common/a?b.py", 4), "vscode://file/work/sample%20%231/common/a%3Fb.py:4");
});

test("demo unreached is global and qualified service filters cannot leak across repos", () => {
  const all = demoFunctions({ hide_handlers: true });
  const helper = all.items.find(row => row.owner.kind === "shared");
  const key = helper.used_in[0].key;
  const scoped = demoFunctions({ hide_handlers: true, used_in: [key] });
  const scopedHelper = scoped.items.find(row => row.symbol.id === helper.symbol.id);
  assert.equal(scopedHelper.min_depth, Math.min(...demoFunctionDetail(helper.symbol.id).apis
    .filter(api => `${api.endpoint.repo_id}/${api.endpoint.service_id}` === key).map(api => api.depth)));
  assert.equal(demoFunctions({ used_in: [helper.used_in[0].service_id] }).total, 0);
  assert.equal(demoFunctions({ used_in: [key], unreached: true }).total, 0);
  assert.ok(!demoFunctions({ unreached: true }).items.some(row => row.symbol.id === helper.symbol.id));
  const duplicate = all.items.find(row => row.symbol.name === "format_value");
  const defined = demoFunctions({ defined_in: [duplicate.owner.key] });
  assert.equal(defined.items.find(row => row.symbol.id === duplicate.symbol.id).same_name_count, 2);
});


test("repository switcher keeps the function view and clears incompatible owner selections", () => {
  const current = { kind: "function", repo: "one", q: "helper", sort: "apis", used: ["one/alpha"],
    defined: ["one/shared/lib"], fn: "old", offset: 50, kindFilter: ["method"] };
  assert.deepEqual(repositoryRoute(current, "two"), { kind: "function", repo: "two", q: "helper", sort: "apis",
    used: undefined, defined: undefined, fn: undefined, offset: 0, kindFilter: ["method"] });
  assert.equal(repositoryRoute(current, "").repo, "");
  assert.deepEqual(repositoryRoute({ kind: "api", id: "old" }, "two"), { kind: "services", repo: "two" });
});
