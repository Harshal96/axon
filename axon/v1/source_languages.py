"""Source extensions recognized by parsing, discovery and incremental state."""

from pathlib import Path

JAVASCRIPT_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
JS_TS_SUFFIXES = (*JAVASCRIPT_SUFFIXES, *TYPESCRIPT_SUFFIXES)
GRAPHQL_SUFFIXES = (".graphql", ".gql")
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".java": "java",
    ".go": "go",
    **dict.fromkeys(JAVASCRIPT_SUFFIXES, "javascript"),
    **dict.fromkeys(TYPESCRIPT_SUFFIXES, "typescript"),
    **dict.fromkeys(GRAPHQL_SUFFIXES, "graphql"),
}


def is_source_path(path: Path) -> bool:
    return path.suffix in LANGUAGE_BY_SUFFIX and not path.name.endswith(
        (".d.ts", ".d.mts", ".d.cts")
    )
