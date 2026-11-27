# Next GraphQL adapters

The first adapter follows an Apollo Client invocation through an SDL field to a TypeScript resolver. Each follow-up below adds one source of evidence to that same path. The proposed order favors reuse of the current parser, then adds code-first schema discovery.

| Delivery | First user-visible result | Independent acceptance test |
| --- | --- | --- |
| 1. Apollo React client | Find the component or event handler that requests a field | A query component and an invoked mutation callback appear as callers. An unused mutation callback contributes zero calls. |
| 2. Python Ariadne provider | Follow a TypeScript caller into a Python resolver and its HTTP dependency | Moving the Python resolver updates its evidence. Removing its registration removes its handler edge. |
| 3. Spring for GraphQL provider | Open an SDL field and navigate to the annotated Java controller method | A root query and mutation each resolve to their own method. A nested object resolver stays outside the endpoint catalog. |
| 4. Python Strawberry provider | Catalog a code-first service from its registered root classes | The catalog observes a renamed field and camel-case configuration. A decorated class outside the registered schema publishes no field. |

## 1. Apollo React

- [ ] Extend the TypeScript extractor to recognize bound imports of `useQuery`, `useLazyQuery`, and `useMutation`. Start with the explicit `client` option, using the existing Apollo client and document binding. Register a possible query from the enclosing component. Trace the execution function returned by the lazy-query and mutation hooks to its call site. [Apollo queries](https://www.apollographql.com/docs/react/data/queries) · [Apollo mutations](https://www.apollographql.com/docs/react/data/mutations)
- [ ] Preserve literal skip, skip-token and cache-only behavior. A shadowed hook, an uncalled execution function, or an unrelated tuple yields zero calls. An evidenced execution with unknown client context produces an external call with a reason. Add provider-context inference in a separate change with fixtures for nested providers.
- [ ] Add a TSX eval that imports an operation and a fragment from separate files. Change the fragment without editing the component and compare a fresh build with incremental publication.

## 2. Python Ariadne

- [ ] Add a focused Python AST extractor beside `parse_python.py`. Record import aliases, `ObjectType("Query")`, `QueryType()`, `@query.field("order")`, and `set_field("order", resolver)`. Resolve direct imports by the existing service-aware Python symbol rules. [Ariadne resolver binding](https://ariadnegraphql.org/server/Docs/resolvers)
- [ ] Follow the specific bindables passed to `make_executable_schema` and the schema passed to the ASGI application. Accept literal SDL and declared SDL files. Feed the registration, binding and mount locations into the shared provider assembler. A computed factory or two conflicting registered bindings should produce a diagnostic.
- [ ] Add a mixed Python/TypeScript fixture with two bindables defining the same name and only one registered. Assert the resolver ID, the cross-language caller, and a downstream REST target reached by that resolver.

## 3. Spring for GraphQL

- [ ] Reuse Java method symbols and class-level annotation evidence in `parse_java.py`. Start with imported Spring `@Controller`, `@QueryMapping`, `@MutationMapping`, `@SubscriptionMapping` and explicit `@SchemaMapping(typeName=…, field=…)`. Honor field-name overrides and the actual parent type named by the annotation. [Spring annotated controllers](https://docs.spring.io/spring-graphql/reference/controllers.html)
- [ ] Index the standard `.graphqls` resource extension through the shared SDL parser and ownership rules. Begin with declared schema resources, then add conventional resource discovery with evidence from the owning Spring project. Treat dynamic component scanning, custom wiring and unknown mounts as gaps.
- [ ] Pin an overloaded method, custom root type, same-named non-Spring annotation and a non-root `Book.author` mapping. Verify an existing TypeScript client can link using a proven mount and that the method's existing Java outbound calls appear.

## 4. Strawberry code-first

- [ ] Add typed root-field declarations as provider inputs with their Python file and line. Follow `strawberry.Schema(query=…, mutation=…, subscription=…)` to explicitly registered types, field decorators, and function references. Record `source=code` and preserve the public type and field names. [Strawberry schema basics](https://strawberry.rocks/docs/general/schema-basics)
- [ ] Cover field-name overrides, the default snake-case to camel-case conversion, and static `auto_camel_case=False`. Build a complete validation schema only when referenced types and wrappers are statically available. Preserve field evidence and report incomplete types in other cases. [Strawberry schema configuration](https://strawberry.rocks/docs/types/schema-configurations)
- [ ] Verify a FastAPI mounting example with an imported root class, a mutation resolver calling an indexed REST service, and a dynamic type factory. Reuse the same field identity for an equivalent code-first or SDL declaration.

## Shared exit criteria

Each adapter ships with its own hand-authored fixture, case, matrix entry, and documentation of supported inputs. Use `FileFacts` for source-local inputs and resolve client targets and provider registrations during assembly. Measure correctness with exact endpoint IDs, handler and call edges, evidence spans, external-call reasons, and cold/warm/incremental equality. Before adding federation, separately design ownership across subgraphs, schema IDs and cross-repository publication. Federation requires evidence for a different ownership model than any of these four adapters.
