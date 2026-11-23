"""Axios transport evidence from synthetic, service-independent JS/TS."""

from axon.v1.parse_javascript import parse_javascript


def calls(source: str, suffix: str = ".ts") -> list[tuple[str, str, str]]:
    facts = parse_javascript("handler" + suffix, source)
    return [(c.method, c.url, c.caller) for c in facts.http_calls if c.via == "axios"]


def test_alias_instance_config_and_absolute_override() -> None:
    assert calls("""
        import { default as transport } from "axios";
        const client = transport.create({ baseURL: process.env.CATALOG, method: "put" });
        const alias = client;
        function run(id: string) {
          const url = `/items/${id}`;
          alias.get<Item>(url);
          alias({url: '/sync'});
          client.request({method: 'patch', url: '/sync', baseURL: 'https://alt.test/v2'});
          alias.postForm('https://other.test/upload', {baseURL: 'not-a-config'});
        }
    """) == [
        ("GET", "{env:CATALOG}/items/{id}", "run"),
        ("PUT", "{env:CATALOG}/sync", "run"),
        ("PATCH", "https://alt.test/v2/sync", "run"),
        ("POST", "https://other.test/upload", "run"),
    ]


def test_commonjs_and_callable_overloads() -> None:
    assert calls(
        """
        const http = require("axios").default;
        const {delete: remove} = http;
        function run() {
          http('/events');
          http('/events', {method: 'post'});
          const config = {url: '/jobs', method: 'patch'};
          http.request(config);
          remove('/jobs/1');
        }
    """,
        ".js",
    ) == [
        ("GET", "/events", "run"),
        ("POST", "/events", "run"),
        ("PATCH", "/jobs", "run"),
        ("DELETE", "/jobs/1", "run"),
    ]


def test_unknown_values_and_config_spread_order() -> None:
    assert calls("""
        import http from 'axios';
        function run(path, method, opts) {
          http.get(path);
          http.request({url: '/fixed', method});
          http.request({url: '/stale', method:'patch', ...opts});
          http.request({...opts, url: 'https://alt.test/fixed', method: 'head',
                        allowAbsoluteUrls: true});
        }
    """) == [
        ("GET", "/{dynamic}", "run"),
        ("", "/fixed", "run"),
        ("", "/{dynamic}", "run"),
        ("HEAD", "https://alt.test/fixed", "run"),
    ]


def test_only_source_proven_receivers_emit_http() -> None:
    assert calls("""
        import http from 'axios';
        import type fake from 'axios';
        function shadow(http) { http.get('/shadow'); }
        function shadowRequire(require) { require('axios').get('/shadow'); }
        function check() {
          { const http = new Map(); http.get('/map'); }
          let reassigned = http; reassigned = new Map(); reassigned.get('/map');
          fake.get('/type-only');
          http.getUri({url: '/build-only'});
          http.interceptors.request.use(() => {});
          http.get('/real');
        }
    """) == [("GET", "/real", "check")]


def test_visible_defaults_writes_leave_affected_fields_unknown() -> None:
    assert calls("""
        import axios from 'axios';
        const api = axios.create({baseURL: 'https://old.test', method: 'delete'});
        const same = api;
        same.defaults.baseURL = chooseBase();
        api.defaults.method = verb;
        axios.defaults.baseURL = remote;
        function run() {
          api.get('/old');
          api('https://external.test/fixed');
          api.post('/fixed', {}, {baseURL: 'https://right.test'});
          axios.get('/old');
        }
    """) == [
        ("GET", "/{dynamic}", "run"),
        ("", "https://external.test/fixed", "run"),
        ("POST", "https://right.test/fixed", "run"),
        ("GET", "/{dynamic}", "run"),
    ]


def test_stable_var_require_and_concatenated_segment() -> None:
    assert calls(
        """
        var transport = require('axios');
        let http = transport;
        function run(id) {
          http.get('/items/' + id + '/info');
          http.get('//vendor.test/v1/status');
          http.get(`https://${hostname}/items`);
        }
    """,
        ".js",
    ) == [
        ("GET", "/items/{id}/info", "run"),
        ("GET", "//vendor.test/v1/status", "run"),
        ("GET", "/{dynamic}", "run"),
    ]


def test_redirected_and_mutated_receivers() -> None:
    assert calls("""
        import http from 'axios';
        const cfg = {url: '/stale'};
        const ref = cfg;
        ref.url = '/changed';
        const client = http.create();
        client.get = () => 123;
        const alias = client;
        const Service = class http {
          static get(key) { return key; }
          run() { return http.get('/class'); }
        };
        function run() {
          http(cfg);
          alias.get('/mock');
          client.put('/real');
        }
    """) == [("", "/{dynamic}", "run"), ("PUT", "/real", "run")]


def test_relative_base_and_unrepresentable_requests_are_retained() -> None:
    assert calls("""
        import http from 'axios';
        const api = http.create({baseURL: 'v1'});
        function run() {
          api.get('items');
          http.get('https://vendor.test/search terms');
          http.get('https:/malformed');
        }
    """) == [
        ("GET", "/v1/items", "run"),
        ("GET", "/{dynamic}", "run"),
        ("GET", "/{dynamic}", "run"),
    ]
