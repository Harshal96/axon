"""Extract HTTP requests from source-proven Axios imports and instances."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Literal

from tree_sitter import Node

from axon.v1.facts import HttpFact
from axon.v1.js_bindings import JSBindings, arguments, field, literal, member, source, unwrap

_VERBS = {
    "get": "GET",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "postForm": "POST",
    "putForm": "PUT",
    "patchForm": "PATCH",
}
_REQUEST_METHODS = frozenset(_VERBS) | {"request"}
_BODY_METHODS = {"POST", "PUT", "PATCH"}
_DYNAMIC = "/{dynamic}"
_Defaults = tuple[str | None, str, bool | None]  # baseURL, method, allowAbsoluteUrls
_DEFAULTS: _Defaults = ("", "GET", True)
_UNKNOWN: Literal["unknown"] = "unknown"


def _request_url(path: str | None, base: str | None, allow_absolute: bool | None) -> str:
    if not path or path == _DYNAMIC:
        return _DYNAMIC
    absolute = re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", path, re.I)
    if absolute and allow_absolute is None and base != "":
        return _DYNAMIC
    if not absolute or allow_absolute is False:
        if base is None:
            return _DYNAMIC
        if base:
            path = base.rstrip("/") + "/" + path.lstrip("/")
    if re.search(r"\s", path):
        return _DYNAMIC
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", path, re.I):
        authority = re.match(r"^(?:https?:)?//([^/?#]+)", path, re.I)
        if not authority or re.search(r"[{}]", authority.group(1)):
            return _DYNAMIC
        return path
    return path if path.startswith(("/", "{env:")) else "/" + path


def apply_axios_defaults(fact: HttpFact, defaults: _Defaults) -> HttpFact:
    """Complete a deferred relative-import call using its source-proven export."""
    base, method, allow = _merge_defaults(defaults, fact.bound)
    return replace(
        fact,
        url=_request_url(fact.url, base, allow),
        bound={},
        method=method if fact.method == "INHERIT" else fact.method,
    )


def _merge_defaults(defaults: _Defaults, override: dict[str, str]) -> _Defaults:
    base, method, allow = defaults
    if "baseURL" in override:
        base = override["baseURL"]
    elif "unknownBase" in override:
        base = None
    if "allowAbsoluteUrls" in override:
        allow = {"true": True, "false": False}.get(override["allowAbsoluteUrls"])
    return base, method, allow


class AxiosCalls:
    def __init__(self, root: Node):
        self.bindings = JSBindings(root)
        self._writes: dict[Node | tuple[str, str], set[str]] = {}
        self._method_writes: dict[Node | tuple[str, str], set[str]] = {}
        for write in self.bindings.member_writes:
            obj, key = member(write)
            if key in _REQUEST_METHODS or key == "create" or not key:
                subject = self._subject(obj)
                if subject is not None:
                    self._method_writes.setdefault(subject, set()).add(key)
            if key == "defaults":
                receiver, key = obj, ""
            else:
                receiver, name = member(self.bindings.stable_expr(obj))
                if name != "defaults":
                    continue
            target = self._subject(receiver)
            if target is not None:
                self._writes.setdefault(target, set()).add(key)

    def _subject(self, node: Node | None) -> Node | tuple[str, str] | None:
        imported = self.bindings.imported(node)
        return imported if imported[0] else self.bindings.stable_expr(node)

    def _written(self, node: Node | None) -> set[str]:
        subject = self._subject(node) if self._writes else None
        return self._writes.get(subject, set()) if subject is not None else set()

    def _after_writes(self, node: Node | None, defaults: _Defaults) -> _Defaults:
        # Assignments in conditionals or other functions may run in any order.
        fields = self._written(node)
        if "" in fields:
            return None, "", None
        base, method, allow = defaults
        return (
            None if "baseURL" in fields else base,
            "" if "method" in fields else method,
            None if "allowAbsoluteUrls" in fields else allow,
        )

    def _value(self, node: Node | None) -> Node | None:
        value = self.bindings.stable_expr(node)
        return value if value and not self.bindings.mutated_object(value) else None

    def _property(self, config: Node | None, key: str) -> Node | Literal["unknown"] | None:
        if config is None:
            return None  # property absent
        obj = self._value(config)
        if not obj or obj.type != "object":
            return _UNKNOWN
        # Later explicit properties override earlier spread/computed properties.
        for prop in reversed(obj.named_children):
            if prop.type == "comment":
                continue
            if prop.type == "pair":
                name = field(prop, "key")
                if name and name.type == "computed_property_name":
                    return _UNKNOWN
                if (literal(name) or source(name)) == key:
                    return field(prop, "value") or _UNKNOWN
            elif prop.type == "shorthand_property_identifier":
                if source(prop) == key:
                    return prop
            else:
                return _UNKNOWN
        return None

    def _text(self, node: Node | None, depth: int = 0) -> str | None:
        if not (node := self._value(node)) or depth >= 16:
            return None
        if node.type == "string":
            return literal(node)
        receiver, name = member(node)
        if name and receiver:
            env, env_name = member(receiver)
            if env_name == "env" and (
                self.bindings.unbound(env, "process")
                or (env and env.type == "meta_property" and source(env) == "import.meta")
            ):
                return "{env:" + name + "}"
        if node.type == "template_string":
            parts = []
            for part in node.named_children:
                if part.type == "string_fragment":
                    parts.append(source(part))
                elif part.type == "template_substitution":
                    values = [c for c in part.named_children if c.type != "comment"]
                    if len(values) != 1:
                        return None
                    value = self._text(values[0], depth + 1)
                    if value is None:
                        # A dynamic path segment is safe only after a visible path prefix.
                        if "/" not in "".join(parts):
                            return None
                        token = source(values[0])
                        value = "{" + (token if re.fullmatch(r"\w+", token) else "var") + "}"
                    parts.append(value)
                else:
                    return None
            return "".join(parts)
        if node.type == "binary_expression" and source(field(node, "operator")) == "+":
            left = self._text(field(node, "left"), depth + 1)
            right = self._text(field(node, "right"), depth + 1)
            term = field(node, "right")
            if right is None and left and "/" in left and term and term.type == "identifier":
                right = "{" + source(term) + "}"
            if left is not None and right is not None:
                return left + right
        return None

    def _overrides(self, config: Node | None) -> dict[str, str]:
        overrides: dict[str, str] = {}
        base = self._property(config, "baseURL")
        if base is not None:
            value = self._text(base) if isinstance(base, Node) else None
            if value is None:
                overrides["unknownBase"] = "true"
            else:
                overrides["baseURL"] = value
        absolute = self._property(config, "allowAbsoluteUrls")
        if absolute is not None:
            flag = self._value(absolute) if isinstance(absolute, Node) else None
            overrides["allowAbsoluteUrls"] = flag.type if flag else "unknown"
        return overrides

    def _method(self, config: Node | None, fallback: str) -> str:
        method = self._property(config, "method")
        if method is None:
            return fallback
        value = literal(self._value(method)) if isinstance(method, Node) else None
        return value.upper() if value and re.fullmatch(r"[a-zA-Z]+", value) else ""

    def client_defaults(self, expr: Node | None, depth: int = 0) -> _Defaults | None:
        """Return the defaults of a proven package import or local create() result."""
        if not (expr := unwrap(expr)) or depth >= 16:
            return None
        module, name = self.bindings.imported(expr)
        if module == "axios" and name == "default":
            return self._after_writes(expr, _DEFAULTS)
        if expr.type == "identifier":
            return self.client_defaults(self.bindings.initializer(expr), depth + 1)
        if expr.type == "call_expression":
            receiver, name = member(field(expr, "function"))
            if (
                name == "create"
                and not self._overwritten(receiver, name)
                and (parent := self.client_defaults(receiver, depth + 1))
            ):
                args = arguments(expr)
                config = args[0] if args else None
                overrides = self._overrides(config)
                base, method, allow = _merge_defaults(parent, overrides)
                defaults = base, self._method(config, method), allow
                return self._after_writes(expr, defaults)
        return None

    def export_defaults(self, expr: Node) -> _Defaults | None:
        # A replaced transport method is local source evidence unavailable to importers.
        if self._method_writes and self._subject(expr) in self._method_writes:
            return None
        return self.client_defaults(expr)

    def _callee(self, call: Node) -> tuple[Node | None, str]:
        fn = unwrap(field(call, "function"))
        receiver, name = member(fn)
        if name in _REQUEST_METHODS:
            return receiver, name
        if fn and fn.type == "identifier":
            receiver, name = self.bindings.destructured(fn)
            if name in _REQUEST_METHODS:
                return receiver, name
        return fn, ""

    def _overwritten(self, expr: Node | None, alias: str) -> bool:
        subject = self._subject(expr) if self._method_writes and alias else None
        writes = self._method_writes.get(subject, set()) if subject is not None else set()
        return "" in writes or alias in writes

    def _request(self, call: Node, caller: str, alias: str) -> HttpFact:
        args = arguments(call)
        first = args[0] if args else None
        method = _VERBS.get(alias, "")
        if method:
            index = 2 if method in _BODY_METHODS else 1
            config = args[index] if len(args) > index else None
            path = self._text(first)
        else:
            value = self._value(first)
            if len(args) > 1 or (
                value and value.type in {"string", "template_string", "binary_expression"}
            ):
                config = args[1] if len(args) > 1 else None
                path = self._text(first)
            else:
                config = first
                url = self._property(config, "url")
                path = self._text(url) if isinstance(url, Node) else None
            method = self._method(config, "INHERIT")
        # Keep the source receiver for diagnostics, without embedding request payloads.
        fn = field(call, "function")
        written = source(fn)
        called = (
            written
            if re.fullmatch(r"[\w$]+(?:\??\.[\w$]+)*", written)
            else ("axios." + alias if alias else "axios")
        )
        if path and re.fullmatch(r"\{env:[^}]+\}", path):
            path = None  # an environment variable alone can contain an arbitrary URL
        return HttpFact(
            caller,
            call.start_point.row + 1,
            method,
            path or _DYNAMIC,
            "axios",
            called,
            bound=self._overrides(config),
        )

    def extract(self, call: Node, caller: str) -> HttpFact | None:
        receiver, alias = self._callee(call)
        defaults = self.client_defaults(receiver)
        if defaults is None or self._overwritten(receiver, alias):
            return None
        return apply_axios_defaults(self._request(call, caller, alias), defaults)

    def imported_call(self, call: Node, caller: str) -> tuple[str, str, HttpFact] | None:
        """Defer relative-import requests until the export has transport evidence."""
        receiver, alias = self._callee(call)
        module, name = self.bindings.imported(receiver, esm_only=True)
        if (
            module.startswith(("./", "../"))
            and name
            and name != "*"
            and not self._overwritten(receiver, alias)
        ):
            fact = self._request(call, caller, alias)
            base, method, allow = self._after_writes(receiver, ("", "INHERIT", True))
            if base is None and "baseURL" not in fact.bound:
                fact.bound["unknownBase"] = "true"
            if allow is None:
                fact.bound.setdefault("allowAbsoluteUrls", "unknown")
            if fact.method == "INHERIT":
                fact.method = method
            return module, name, fact
        return None
