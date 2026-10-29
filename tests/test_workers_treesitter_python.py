"""
Tests for the Python tree-sitter parser.
Pure unit tests — no Neo4j required.
"""

from __future__ import annotations

from pathlib import Path

from axon.models.enums import SymbolKind
from axon.workers.treesitter.python import PythonParser

FIXTURES = Path(__file__).parent / "fixtures" / "sample_python"


def _parse(filename: str):
    parser = PythonParser()
    return parser.parse_file(filename, str(FIXTURES.parent.parent))


# ── Symbol extraction ──────────────────────────────────────────────────────────


def test_parse_payment_finds_class() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    class_symbols = [s for s in parsed.symbols if s.kind == SymbolKind.CLASS]
    assert len(class_symbols) == 1
    assert class_symbols[0].name == "PaymentService"


def test_parse_payment_finds_methods() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    methods = [s for s in parsed.symbols if s.kind == SymbolKind.METHOD]
    method_names = {m.name for m in methods}
    assert "charge" in method_names
    assert "_validate_amount" in method_names
    assert "refund" in method_names
    assert "__init__" in method_names


def test_parse_payment_finds_function() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    functions = [s for s in parsed.symbols if s.kind == SymbolKind.FUNCTION]
    assert len(functions) == 1
    assert functions[0].name == "create_payment_service"


def test_private_method_not_public() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    validate = next(s for s in parsed.symbols if s.name == "_validate_amount")
    assert validate.is_public is False


def test_public_method_is_public() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    charge = next(s for s in parsed.symbols if s.name == "charge")
    assert charge.is_public is True


def test_class_docstring_extracted() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    cls = next(s for s in parsed.symbols if s.name == "PaymentService")
    assert cls.docstring is not None
    assert "payment processing" in cls.docstring.lower()


def test_method_has_parent_class() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    charge = next(s for s in parsed.symbols if s.name == "charge")
    assert charge.parent_class == "PaymentService"


def test_function_has_no_parent_class() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    factory = next(s for s in parsed.symbols if s.name == "create_payment_service")
    assert factory.parent_class is None


def test_method_signature_extracted() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    charge = next(s for s in parsed.symbols if s.name == "charge")
    assert charge.signature is not None
    assert "amount" in charge.signature


# ── Import extraction ──────────────────────────────────────────────────────────


def test_parse_payment_finds_imports() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    assert len(parsed.imports) >= 2
    modules = [imp.module_path for imp in parsed.imports]
    assert "internal.models.models" in modules


def test_parse_checkout_imports_payment() -> None:
    parsed = _parse("fixtures/sample_python/checkout.py")
    assert parsed is not None
    modules = [imp.module_path for imp in parsed.imports]
    assert "services.payment.payment" in modules


def test_import_names_captured() -> None:
    parsed = _parse("fixtures/sample_python/checkout.py")
    assert parsed is not None
    payment_import = next(
        imp for imp in parsed.imports if imp.module_path == "services.payment.payment"
    )
    assert "PaymentService" in payment_import.names
    assert "create_payment_service" in payment_import.names


# ── Call extraction ────────────────────────────────────────────────────────────


def test_parse_payment_finds_calls() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    assert len(parsed.calls) > 0


def test_charge_calls_validate() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    charge_scope = "fixtures/sample_python/payment.py::PaymentService.charge"
    charge_calls = [c for c in parsed.calls if c.caller_symbol_id == charge_scope]
    called_names = {c.called_name for c in charge_calls}
    assert "self._validate_amount" in called_names or "_validate_amount" in called_names


# ── Edge cases ─────────────────────────────────────────────────────────────────


def test_parse_nonexistent_file_returns_none() -> None:
    parser = PythonParser()
    result = parser.parse_file("does/not/exist.py", "/tmp")
    assert result is None


def test_sha256_computed() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    assert len(parsed.sha256) == 64  # hex-encoded SHA-256


def test_line_count_positive() -> None:
    parsed = _parse("fixtures/sample_python/payment.py")
    assert parsed is not None
    assert parsed.line_count > 0
