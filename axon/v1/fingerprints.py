"""Content-addressed implementation versions, stable across formatting edits.

Manual epochs remain available for changes outside the fingerprinted modules.
No imports of implementation modules: safe for parsers, state and API to share.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

PARSER_EPOCHS = {".py": 11, ".java": 5, ".go": 2}
INDEXER_VERSION = 1
STATE_VERSION = 1
_ROOT = Path(__file__).parent


def module_fingerprint(source: str) -> str:
    tree = ast.parse(source)
    return hashlib.sha256(repr(_canonical_ast(tree)).encode()).hexdigest()


def _canonical_ast(value: object) -> object:
    """Avoid version-specific ast.dump formatting and empty AST fields.

    Python 3.12 adds empty type_params fields; 3.13 changes dump's treatment
    of empty fields. Retain populated fields so type parameters still affect
    cache compatibility, while equivalent source hashes alike on all hosts.
    """
    if isinstance(value, ast.AST):
        return (
            type(value).__name__,
            tuple(
                (name, _canonical_ast(field))
                for name, field in sorted(ast.iter_fields(value))
                if field is not None and field != []
            ),
        )
    if isinstance(value, list):
        return tuple(_canonical_ast(item) for item in value)
    return value


def _modules(names: list[str], extra: object = None) -> str:
    data = [module_fingerprint((_ROOT / (name + ".py")).read_text()) for name in names]
    return hashlib.sha256(json.dumps([data, extra], sort_keys=True).encode()).hexdigest()


PARSER_FINGERPRINTS = {
    suffix: _modules(
        ["parse_" + lang, "facts", "source_ranges"]
        + (["java_constants"] if lang == "java" else ["sdk_catalog"] if lang == "python" else []),
        PARSER_EPOCHS[suffix],
    )
    for suffix, lang in {".py": "python", ".java": "java", ".go": "go"}.items()
}
INDEXER_FINGERPRINT = _modules(
    [
        "indexer",
        "linker",
        "manual_corrections",
        "framework_hooks",
        "client_discovery",
        "servicemap",
        "sdk_catalog",
        "java_constants",
        "graph",
        "models",
        "incremental",
        "indexstate",
        "swagger",
    ],
    [INDEXER_VERSION, PARSER_FINGERPRINTS],
)
# DDL and serialization live together; hashing the module also catches codec changes.
STATE_FINGERPRINT = _modules(["indexstate"], STATE_VERSION)


def build_versions() -> str:
    return json.dumps(
        {
            "indexer": INDEXER_FINGERPRINT,
            "parsers": PARSER_FINGERPRINTS,
            "state": STATE_FINGERPRINT,
        },
        sort_keys=True,
    )
