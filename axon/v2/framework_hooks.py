"""Conservative source-visible framework flow, separate from literal calls."""

from __future__ import annotations

from typing import TYPE_CHECKING

from axon.v2.graph import GraphSnapshot
from axon.v2.models import Confidence, EdgeKind, Evidence, GraphEdge, symbol_node_id

if TYPE_CHECKING:
    from axon.v2.indexer import _CallResolver

# Hooks run by DRF implementations that are ordinarily outside the repository.
ACTION_HOOKS = {
    "create": ("perform_create",),
    "update": ("perform_update",),
    "partial_update": ("perform_update",),
    "destroy": ("perform_destroy",),
    "list": (),
    "retrieve": (),
}
SERIALIZER_HOOKS = {
    "create": ("validate", "create"),
    "update": ("validate", "update"),
    "partial_update": ("validate", "update"),
    "list": ("to_representation",),
    "retrieve": ("to_representation",),
}
GENERIC_ACTIONS = {
    "CreateAPIView": ("POST", "create"),
    "ListAPIView": ("GET", "list"),
    "RetrieveAPIView": ("GET", "retrieve"),
    "UpdateAPIView": ("PUT", "update"),
    "DestroyAPIView": ("DELETE", "destroy"),
}


def add_framework_calls(snap: GraphSnapshot, repo: str, resolver: _CallResolver) -> None:
    def lineage(path: str, cls: str, seen: set[tuple[str, str]]) -> list[tuple[str, str]]:
        if (path, cls) in seen or len(seen) > 25:
            return []
        seen.add((path, cls))
        out = [(path, cls)]
        for base in resolver.facts[path].superclasses.get(cls, []):
            for p, q in resolver.resolve(base, path, cls):
                out.extend(lineage(p, q, seen))
        return out

    def add(source: str, path: str, qualified: str) -> None:
        target = symbol_node_id(repo, path, qualified)
        if target in snap.nodes and source != target:
            snap.add_edge(
                GraphEdge(
                    from_id=source,
                    to_id=target,
                    kind=EdgeKind.CALLS,
                    confidence=Confidence.INFERRED,
                    evidence=Evidence(
                        file=path, line=snap.nodes[target].line_start, rule="framework:drf-implicit"
                    ),
                )
            )

    def serializers(path: str, cls: str) -> list[tuple[str, str]]:
        queue = [(path, cls)]
        seen: set[tuple[str, str]] = set()
        while queue and len(seen) < 25:
            p, q = queue.pop(0)
            if (p, q) in seen:
                continue
            seen.add((p, q))
            for bp, bq in lineage(p, q, set()):
                for binding in resolver.facts[bp].bindings:
                    if binding.caller != bq:
                        continue
                    for np, nq in resolver.resolve(binding.source, bp, bq):
                        bases = [
                            resolver.canonical(base, lp)
                            for lp, lq in lineage(np, nq, set())
                            for base in resolver.facts[lp].superclasses.get(lq, [])
                        ]
                        if any(base.startswith("rest_framework.serializers.") for base in bases):
                            queue.append((np, nq))
        if queue:
            snap.diagnostics.append(f"{path}:{cls}: nested serializer targets truncated at 25")
        return list(seen)

    for edge in list(snap.edges):
        if edge.kind != EdgeKind.HANDLES:
            continue
        handler = snap.nodes[edge.from_id]
        endpoint = snap.nodes[edge.to_id]
        if endpoint.props.get("framework") != "django":
            continue
        qualified = handler.props.get("qualified", "")
        cls = (
            qualified
            if (handler.file, qualified) in resolver.symbols
            and resolver.symbols[(handler.file, qualified)].kind == "CLASS"
            else qualified.rpartition(".")[0]
        )
        if not cls or handler.file not in resolver.facts:
            continue
        classes = lineage(handler.file, cls, set())
        bases = [
            resolver.canonical(b, p)
            for p, q in classes
            for b in resolver.facts[p].superclasses.get(q, [])
        ]
        if not any(b.startswith("rest_framework.") for b in bases):
            continue
        action = handler.name
        method = endpoint.props.get("method", "ANY")
        if action not in ACTION_HOOKS:
            action = next(
                (
                    a
                    for b in bases
                    for name, (verb, a) in GENERIC_ACTIONS.items()
                    if b.rsplit(".", 1)[-1] == name and method in {verb, "ANY"}
                ),
                "",
            ) or {
                "POST": "create",
                "PUT": "update",
                "PATCH": "partial_update",
                "DELETE": "destroy",
                "GET": "retrieve",
            }.get(method, "")
        generic = any(
            b.startswith(("rest_framework.generics.", "rest_framework.mixins."))
            or b
            in {
                "rest_framework.viewsets.GenericViewSet",
                "rest_framework.viewsets.ModelViewSet",
                "rest_framework.viewsets.ReadOnlyModelViewSet",
            }
            for b in bases
        )
        framework_body = (
            handler.props.get("kind") == "CLASS"
            or handler.props.get("stub") == "true"
            or any(
                call.caller == qualified
                and call.called in {"super." + handler.name, "self." + action, "super." + action}
                for call in resolver.facts[handler.file].calls
            )
        )
        generic = generic and framework_body
        hooks: tuple[str, ...] = ("initial", "check_permissions")
        if generic:
            if action in {"list", "retrieve", "update", "partial_update", "destroy"}:
                hooks += ("get_queryset", "filter_queryset")
            if action in {"retrieve", "update", "partial_update", "destroy"}:
                hooks += ("get_object",)
            if action == "list":
                hooks += ("paginate_queryset",)
            if action in SERIALIZER_HOOKS:
                hooks += ("get_serializer_class", "get_serializer_context")
            hooks += ACTION_HOOKS.get(action, ())
        for hook in hooks:
            # _methods implements first source-visible override along inheritance.
            for p, q in resolver._methods([(handler.file, cls)], hook, set()):
                add(handler.id, p, q)
        configured: set[str] = set()
        for p, q in classes:
            attributes = resolver.bindings.get((p, q), {})
            for attr, ref in attributes.items():
                if attr in configured:
                    continue
                configured.add(attr)
                if attr == "serializer_class" and generic:
                    serializer_types = [
                        nested
                        for cp, cq in resolver.resolve(ref, p, q)
                        for nested in serializers(cp, cq)
                    ]
                    for sp, sq in serializer_types:
                        for name in SERIALIZER_HOOKS.get(action, ()):
                            for mp, mq in resolver._methods([(sp, sq)], name, set()):
                                add(handler.id, mp, mq)
                        for sp2, sq2 in lineage(sp, sq, set()):
                            method_fields = {
                                "get_" + binding.var
                                for binding in resolver.facts[sp2].bindings
                                if binding.caller == sq2
                                and binding.source.rsplit(".", 1)[-1] == "SerializerMethodField"
                            }
                            for binding in resolver.facts[sp2].bindings:
                                if (
                                    binding.caller != sq2
                                    or binding.source.rsplit(".", 1)[-1] != "SerializerMethodField"
                                ):
                                    continue
                                for call in resolver.facts[sp2].calls:
                                    if (
                                        call.caller == sq2
                                        and call.line == binding.line
                                        and call.arguments.get("method_name")
                                    ):
                                        method_fields.discard("get_" + binding.var)
                                        method_fields.add(call.arguments["method_name"])
                            for symbol in resolver.facts[sp2].symbols:
                                if (
                                    symbol.qualified.startswith(sq2 + ".")
                                    and symbol.kind == "METHOD"
                                ):
                                    if (
                                        action in {"create", "update", "partial_update"}
                                        and symbol.name.startswith("validate_")
                                    ) or (
                                        action in {"list", "retrieve"}
                                        and symbol.name in method_fields
                                    ):
                                        add(handler.id, sp2, symbol.qualified)
                elif attr in {
                    "permission_classes",
                    "filter_class",
                    "filterset_class",
                    "pagination_class",
                }:
                    for typ in resolver.receiver_types(ref, p, q):
                        for cp, cq in resolver.resolve(typ, p, q):
                            names: tuple[str, ...] = ()
                            if attr == "permission_classes":
                                names = ("has_permission",)
                                if generic and action in {
                                    "retrieve",
                                    "update",
                                    "partial_update",
                                    "destroy",
                                }:
                                    names += ("has_object_permission",)
                            elif (
                                generic
                                and attr in {"filter_class", "filterset_class"}
                                and action != "create"
                            ):
                                names = ("filter_queryset",)
                            elif generic and attr == "pagination_class" and action == "list":
                                names = ("paginate_queryset",)
                            for name in names:
                                for mp, mq in resolver._methods([(cp, cq)], name, set()):
                                    add(handler.id, mp, mq)

    # Index senders once; never rescan every file for each save/delete call.
    signal_methods = {"save": {"pre_save", "post_save"}, "delete": {"pre_delete", "post_delete"}}
    signal_index: dict[tuple[str, str, str], list[tuple[str, str, int, str, str]]] = {}
    for source in resolver.facts.values():
        for signal in source.signals:
            canonical = resolver.canonical(signal.signal, source.path)
            if not canonical.startswith("django.db.models.signals."):
                continue
            members = [
                m for m, names in signal_methods.items() if canonical.rsplit(".", 1)[-1] in names
            ]
            for sender_path, sender_name in resolver.resolve(signal.sender, source.path):
                for p, q in resolver.resolve(signal.receiver, source.path):
                    for member in members:
                        signal_index.setdefault((sender_path, sender_name, member), []).append(
                            (p, q, signal.line, canonical, source.path)
                        )
    if not signal_index:
        return
    for facts in resolver.facts.values():
        for call in facts.calls:
            receiver, _, member = call.called.rpartition(".")
            if member not in signal_methods or not call.caller:
                continue
            types = resolver.receiver_types(receiver, facts.path, call.caller)
            model_ids = {
                hit
                for typ in types
                for hit in resolver.resolve(typ, facts.path, call.caller)
                if any(
                    resolver.canonical(base, p)
                    in {"django.db.models.Model", "django.db.models.base.Model"}
                    for p, q in lineage(hit[0], hit[1], set())
                    for base in resolver.facts[p].superclasses.get(q, [])
                )
            }
            targets = [
                target for p, q in model_ids for target in signal_index.get((p, q, member), [])
            ]
            if len(targets) > 25:
                snap.diagnostics.append(f"{facts.path}:{call.line}: signal targets truncated at 25")
            for p, q, line, canonical, evidence_path in targets[:25]:
                from_id = symbol_node_id(repo, facts.path, call.caller)
                to_id = symbol_node_id(repo, p, q)
                if from_id in snap.nodes and to_id in snap.nodes:
                    snap.add_edge(
                        GraphEdge(
                            from_id=from_id,
                            to_id=to_id,
                            kind=EdgeKind.CALLS,
                            confidence=Confidence.AMBIGUOUS,
                            evidence=Evidence(
                                file=evidence_path,
                                line=line,
                                rule="framework:django-signal",
                                detail=canonical,
                            ),
                        )
                    )
