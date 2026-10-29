from __future__ import annotations

from pathlib import Path

from axon.workers.ts.parser import parse_ts_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_imports_tsx() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    specs = [i.module_specifier for i in result.imports]
    assert "react" in specs
    assert "@company/ui-components" in specs
    assert "./usePayment" in specs


def test_parse_named_imports() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    ui_imp = next(i for i in result.imports if i.module_specifier == "@company/ui-components")
    assert "Button" in ui_imp.named_imports
    assert "Input" in ui_imp.named_imports


def test_parse_type_only_import() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    type_imp = next(
        (i for i in result.imports if "CheckoutFormProps" in i.named_imports),
        None,
    )
    assert type_imp is not None
    assert type_imp.is_type_only is True


def test_parse_react_component() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    component = next((s for s in result.symbols if s.name == "CheckoutForm"), None)
    assert component is not None
    assert component.kind == "REACT_COMPONENT"
    assert component.is_exported is True


def test_parse_interface() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    iface = next((s for s in result.symbols if s.name == "FormState"), None)
    assert iface is not None
    assert iface.kind == "INTERFACE"


def test_parse_hook_function() -> None:
    result = parse_ts_file(str(FIXTURES / "usePayment.ts"))
    hook = next((s for s in result.symbols if s.name == "usePayment"), None)
    assert hook is not None
    assert hook.kind in ("FUNCTION", "ARROW_FUNCTION")
    assert hook.is_exported is True


def test_parse_re_exports() -> None:
    result = parse_ts_file(str(FIXTURES / "index.ts"))
    re_exports = [e for e in result.exports if e.is_re_export]
    source_modules = {e.source_module for e in re_exports}
    assert "./CheckoutForm" in source_modules
    assert "./usePayment" in source_modules


def test_jsx_tags_captured() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    component = next(s for s in result.symbols if s.name == "CheckoutForm")
    assert "Button" in component.jsx_tags_used
    assert "Input" in component.jsx_tags_used


def test_no_parse_errors_tsx() -> None:
    result = parse_ts_file(str(FIXTURES / "CheckoutForm.tsx"))
    assert result.has_parse_errors is False


def test_no_parse_errors_ts() -> None:
    result = parse_ts_file(str(FIXTURES / "usePayment.ts"))
    assert result.has_parse_errors is False
