"""
Base types for AST parsers. Every language-specific parser returns ParsedFile objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from axon.models.enums import Language, SymbolKind


@dataclass
class ParsedSymbol:
    """A single code symbol extracted from a source file."""

    symbol_id: str  # stable: 'file_path::ClassName.method_name'
    name: str  # short name, e.g. 'charge'
    qualified_name: str  # e.g. 'payment.PaymentService.charge'
    kind: SymbolKind
    line_start: int
    line_end: int
    docstring: str | None = None
    signature: str | None = None
    parent_class: str | None = None
    is_public: bool = True
    is_test: bool = False


@dataclass
class ParsedImport:
    """An import statement extracted from a source file."""

    module_path: str  # e.g. 'services.payment.payment' or 'os.path'
    names: list[str] = field(default_factory=list)  # e.g. ['PaymentService', 'ChargeResult']
    is_relative: bool = False
    line: int = 0


@dataclass
class ParsedCall:
    """A function/method call extracted from a source file."""

    caller_symbol_id: str  # symbol_id of the function/method containing this call
    called_name: str  # e.g. 'charge' or 'PaymentService.charge'
    line: int = 0


@dataclass
class ParsedFile:
    """Complete parse result for a single source file."""

    path: str  # repo-relative
    language: Language
    sha256: str
    line_count: int
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)


class BaseAstParser(ABC):
    """Interface for language-specific parsers."""

    @abstractmethod
    def parse_file(self, file_path: str, repo_root: str) -> ParsedFile | None: ...
