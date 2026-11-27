import json

import pytest

from axon.v1.js_apollo import apollo_inputs
from axon.v1.models import symbol_node_id
from axon.v1.parse_graphql import parse_graphql
from axon.v1.parse_javascript import parse_javascript

pytestmark = pytest.mark.tier1


def inputs(**sources):
    files = [
        parse_graphql(path, text)
        if path.endswith((".graphql", ".gql"))
        else parse_javascript(path, text)
        for path, text in sources.items()
    ]
    # Raw JS facts can be cached and replayed under another path.
    for f in files:
        assert f.apollo == json.loads(json.dumps(f.apollo))
    return apollo_inputs("r", files, {f.path: ("service", "svc") for f in files})


def test_registered_schema_resolvers_and_mount_use_lexical_evidence():
    servers, calls = inputs(
        **{
            "server.ts": """import { ApolloServer as Server } from '@apollo/server';
import {expressMiddleware as mount} from '@as-integrations/express5';
import express from 'express';
import {schema, handlers} from './schema.js';
const app = express();
const registered = new Server({typeDefs: schema, resolvers: handlers});
app.use('/api/graphql', mount(registered));
function shadow(registered) {app.use('/wrong', mount(registered))}
""",
            "schema.ts": """import gql from 'graphql-tag';
import {read as imported} from './orders';
export const schema = gql`type Query {
    order: String, inline: Int, short: Int, dynamic: Int
}`;
const unrelated = {Query: {inline() {return 'wrong'}}};
const short = () => 0;
export const handlers = {Query: {
  order: imported,
  inline() {return 1},
  short,
  dynamic: choose(),
}};
""",
            "orders.ts": "export function read() {return 'id'}",
        }
    )
    (s,) = servers
    assert calls == []
    assert (s.service_id, s.file, s.line, s.mounts) == ("svc", "server.ts", 6, ["/api/graphql"])
    assert [(doc.path, doc.line, doc.column) for doc in s.documents] == [("schema.ts", 3, 27)]
    assert s.schema_complete
    bindings = {b.coordinate: b for b in s.resolvers}
    assert bindings["Query.order"].symbol_id == symbol_node_id("r", "orders.ts", "read")
    assert bindings["Query.inline"].symbol_id == symbol_node_id(
        "r", "schema.ts", "handlers.Query.inline"
    )
    assert bindings["Query.short"].symbol_id == symbol_node_id("r", "schema.ts", "short")
    assert bindings["Query.dynamic"].symbol_id == ""
    assert (bindings["Query.order"].file, bindings["Query.order"].line) == ("schema.ts", 9)
    assert {"schema.ts", "orders.ts"} <= set(s.dependencies)


def test_imported_client_composed_fragments_shadowed_invocations_and_cache_policy():
    _, calls = inputs(
        **{
            "app.ts": """import {api} from './api';
import {request} from './doc';
export function run() {
  api.query({query: request}); api.query({query: request, fetchPolicy: 'network-only'});
  api.mutate({mutation: dynamic()});
}
export function shadow(api) { api.query({query: request}) }
""",
            "api.ts": """import {ApolloClient as Client, HttpLink} from '@apollo/client';
const link = new HttpLink({uri: 'https://orders.test/custom'});
export const api = new Client({link, defaultOptions: {query: {fetchPolicy: 'cache-only'}}});
""",
            "doc.ts": """import {gql as tag} from '@apollo/client';
import {fields} from './fields';
export const request = tag`query MyPage { ...MyFields }
${fields}`;
""",
            "fields.ts": """import {gql} from '@apollo/client/core';
export const fields = gql`fragment MyFields on Query { id }`;""",
        }
    )
    assert [(c.kind, c.line, c.column, c.cache_only) for c in calls] == [
        ("query", 4, 3, True),
        ("query", 4, 32, False),
        ("mutation", 5, 3, False),
    ]
    assert {c.caller_id for c in calls} == {symbol_node_id("r", "app.ts", "run")}
    assert {c.uri for c in calls} == {"https://orders.test/custom"}
    assert [d.path for d in calls[0].documents] == ["doc.ts", "fields.ts"]
    assert calls[0].documents[0].text.splitlines()[0] == "query MyPage { ...MyFields }"
    assert calls[1].documents == calls[0].documents
    assert calls[2].documents == []
    assert {"api.ts", "doc.ts", "fields.ts"} <= set(calls[0].dependencies)


def test_opaque_inputs_remain_opaque_without_turning_names_into_evidence():
    servers, calls = inputs(
        **{
            "x.ts": """import {ApolloClient, ApolloServer, gql} from 'unrelated';
new ApolloServer({typeDefs: gql`type Query {ok: Int}`});
new ApolloClient({uri:'/graphql'}).query({query: gql`{ok}`});
""",
            "y.ts": """import {ApolloClient, gql} from '@apollo/client';
const real = new ApolloClient({uri: location()});
real.query({query: gql`query { ${compute()} }`});
real.query({query: '{bad}'});
function shadow(gql) {real.query({query:gql`{bad}`});}
""",
        }
    )
    assert not servers
    assert len(calls) == 3
    assert all(c.uri == "" and c.documents == [] for c in calls)


def test_standalone_document_imports_partial_schema_and_separate_mount_file():
    (server,), (query,) = inputs(
        **{
            "orders/server.ts": """import {ApolloServer} from '@apollo/server';
import schema from './order.graphql';
export const server = new ApolloServer({typeDefs: [schema, makeExtension()]});""",
            "orders/mount.ts": """import express from 'express';
import {expressMiddleware} from '@apollo/server/express4';
import {server} from './server';
express().use('/g', expressMiddleware(server));""",
            "orders/order.graphql": "type Query { order: Int }",
            "web/api.ts": """import {ApolloClient} from '@apollo/client';
import lookup from './query.gql';
new ApolloClient({uri:'https://orders/g'}).query({query:lookup});""",
            "web/query.gql": "{ order }",
        }
    )
    assert server.mounts == ["/g"]
    assert [d.path for d in server.documents] == ["orders/order.graphql"]
    assert not server.schema_complete
    assert "orders/mount.ts" in server.dependencies
    assert [d.path for d in query.documents] == ["web/query.gql"]
    assert query.caller_id == symbol_node_id("r", "web/api.ts", "<module>")


def test_overridden_transport_and_computed_context():
    _, calls = inputs(
        **{
            "x.ts": """import {ApolloClient, HttpLink, gql} from '@apollo/client';
const api = new ApolloClient({link:new HttpLink({uri:'https://first/g'}), uri:'https://unused/g'});
const query = gql`query Page { id }`;
api.query({query, context:{uri:'https://second/g'}});
api.query({query, context: pickContext()});
api.query({query, context:{uri: unknown}});
api.query({query, context:{headers:{a:'b'}}});"""
        }
    )
    assert [c.uri for c in calls] == ["https://second/g", "", "", "https://first/g"]


def test_duplicate_or_mutated_resolver_map_does_not_claim_a_handler():
    servers, _ = inputs(
        **{
            "s.ts": """import {ApolloServer} from '@apollo/server';
const typeDefs = 'type Query { id: Int }';
const dubious = {Query: {id() {return 1}, id() {return 2}}};
new ApolloServer({typeDefs, resolvers:dubious});
const changed = {Query: {id() {return 3}}};
changed.Query.id = getHandler();
new ApolloServer({typeDefs, resolvers:changed});"""
        }
    )
    assert len(servers) == 2
    assert all(not b.symbol_id for s in servers for b in s.resolvers)


def test_interpolations_in_comment_and_within_selections_are_unresolved():
    _, calls = inputs(
        **{
            "client.ts": """import {ApolloClient, gql} from '@apollo/client';
const api = new ApolloClient({uri:'https://remote/g'});
const fields = gql`fragment Fields on Query { id }`;
api.query({query:gql`{ ...Fields } # ${fields}`});
api.query({query:gql`{ id ${fields} }`});
api.query({query:gql`${fields} query { ...Fields }`});"""
        }
    )
    assert [len(c.documents) for c in calls] == [0, 0, 2]


@pytest.mark.parametrize(
    "override", ["{Query: {...dynamic()}}", "{[computed()]: {f: replacement}}", "{Query: unknown}"]
)
def test_dynamic_resolver_array_entry_can_override_another_map(override):
    (s,), _ = inputs(
        **{
            "s.ts": """import {ApolloServer} from '@apollo/server';
const typeDefs = 'type Query { f: Int }';
function old() { return 1 }
new ApolloServer({typeDefs, resolvers:[{Query:{f: old}}, OVERRIDE]});""".replace(
                "OVERRIDE", override
            )
        }
    )
    bindings = {b.symbol_id for b in s.resolvers if b.coordinate == "Query.f"}
    assert not bindings or "" in bindings
