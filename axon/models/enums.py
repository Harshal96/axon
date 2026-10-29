from enum import StrEnum


class Language(StrEnum):
    PYTHON = "PYTHON"
    JAVA = "JAVA"
    JAVASCRIPT = "JAVASCRIPT"
    TYPESCRIPT = "TYPESCRIPT"
    GENERATED = "GENERATED"  # output of a genrule; language may be any of the above


class SymbolKind(StrEnum):
    FUNCTION = "FUNCTION"
    CLASS = "CLASS"
    METHOD = "METHOD"
    VARIABLE = "VARIABLE"
    INTERFACE = "INTERFACE"
    ENUM = "ENUM"
    CONSTANT = "CONSTANT"
    MODULE = "MODULE"


class BazelTargetKind(StrEnum):
    PY_LIBRARY = "PY_LIBRARY"
    PY_BINARY = "PY_BINARY"
    PY_TEST = "PY_TEST"
    JAVA_LIBRARY = "JAVA_LIBRARY"
    JAVA_BINARY = "JAVA_BINARY"
    JAVA_TEST = "JAVA_TEST"
    JS_LIBRARY = "JS_LIBRARY"
    JS_BINARY = "JS_BINARY"
    GENRULE = "GENRULE"
    MACRO = "MACRO"
    TEST_SUITE = "TEST_SUITE"
    FILEGROUP = "FILEGROUP"
    PROTO_LIBRARY = "PROTO_LIBRARY"
    UNKNOWN = "UNKNOWN"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class EdgeConfidence(StrEnum):
    EXTRACTED = "EXTRACTED"  # directly parsed from source/BUILD
    INFERRED = "INFERRED"  # derived from cross-language or indirect analysis
    AMBIGUOUS = "AMBIGUOUS"  # uncertain; use with low weight


class DependencyType(StrEnum):
    DIRECT = "DIRECT"
    TRANSITIVE = "TRANSITIVE"


class ImportType(StrEnum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    CONDITIONAL = "CONDITIONAL"


class MavenScope(StrEnum):
    COMPILE = "COMPILE"
    TEST = "TEST"
    PROVIDED = "PROVIDED"
    RUNTIME = "RUNTIME"


class GenruleOutputStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
