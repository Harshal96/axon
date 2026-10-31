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
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


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
        "framework_hooks",
        "client_discovery",
        "servicemap",
        "sdk_catalog",
        "java_constants",
        "graph",
        "models",
        "incremental",
        "indexstate",
        "../workers/swagger/parser",
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
