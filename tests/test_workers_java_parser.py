from __future__ import annotations

from pathlib import Path

from axon.workers.java.parser import parse_java_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_package() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    assert result.package == "com.company.payment"


def test_parse_imports() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    import_names = [i.qualified_name for i in result.imports]
    assert "com.company.models.ChargeResult" in import_names
    assert "java.math.BigDecimal" in import_names
    assert "org.apache.commons.collections.CollectionUtils" in import_names


def test_parse_outer_class() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    classes = [s for s in result.symbols if s.kind == "CLASS"]
    names = [c.name for c in classes]
    assert "PaymentGateway" in names


def test_parse_inner_class() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    inner = [s for s in result.symbols if s.name == "RetryPolicy" and s.kind == "CLASS"]
    assert len(inner) == 1
    assert "PaymentGateway" in inner[0].parent_class


def test_parse_methods() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    method_names = [s.name for s in result.symbols if s.kind == "METHOD"]
    assert "charge" in method_names
    assert "submitToGateway" in method_names
    assert "validate" in method_names


def test_parse_constructor() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    constructors = [s for s in result.symbols if s.kind == "CONSTRUCTOR"]
    assert any(c.name == "PaymentGateway" for c in constructors)


def test_parse_extends_implements() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    outer = next(s for s in result.symbols if s.name == "PaymentGateway")
    assert "AbstractPaymentProcessor" in outer.extends
    assert "PaymentProcessor" in outer.implements
    assert "Auditable" in outer.implements


def test_public_vs_private_methods() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    charge = next(s for s in result.symbols if s.name == "charge")
    submit = next(s for s in result.symbols if s.name == "submitToGateway")
    assert charge.is_public is True
    assert submit.is_public is False


def test_fields_extracted() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    fields = [s for s in result.symbols if s.kind == "FIELD"]
    field_names = [f.name for f in fields]
    assert "gatewayUrl" in field_names
    assert "timeoutMs" in field_names


def test_no_parse_errors() -> None:
    result = parse_java_file(str(FIXTURES / "PaymentGateway.java"))
    assert result.parse_errors == 0
