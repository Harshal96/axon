from __future__ import annotations

import json
from pathlib import Path

from axon.v2.onboard import scan_repo


def _spec(root: Path, rel: str, title: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"swagger": "2.0", "info": {"title": title, "version": "1"}, "paths": {"/x": {}}}
        ),
        encoding="utf-8",
    )


def _code(root: Path, rel: str, text: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _roots(report) -> set[str]:
    return {s["roots"][0] for s in report.services}


def test_specs_at_any_depth_become_services(tmp_path: Path) -> None:
    _spec(tmp_path, "services/alpha/swagger.json", "Alpha API")
    _code(tmp_path, "services/alpha/app.py")
    # Spec buried in a conventional resource tree, service 3 levels down.
    _spec(tmp_path, "platform/team/beta/src/main/resources/swagger.json", "Beta API")
    _code(tmp_path, "platform/team/beta/src/main/java/com/x/App.java", "@SpringBootApplication\n")
    (tmp_path / "platform/team/beta/pom.xml").write_text("<project/>", encoding="utf-8")
    # Spec in an api/ subdir, service outside services/.
    _spec(tmp_path, "tools/gamma/api/openapi.yaml", "Gamma API")
    _code(tmp_path, "tools/gamma/main.go", "package main\nfunc main() {}\n")
    _code(tmp_path, "tools/gamma/go.mod", "module gamma\n")

    roots = _roots(scan_repo(tmp_path))
    assert "services/alpha" in roots
    assert "platform/team/beta" in roots
    assert "tools/gamma" in roots


def test_worktrees_are_not_scanned(tmp_path: Path) -> None:
    _spec(tmp_path, "services/alpha/swagger.json", "Alpha API")
    _code(tmp_path, "services/alpha/app.py")
    _spec(tmp_path, ".worktrees/wip/services/ghost/swagger.json", "Ghost API")
    _code(tmp_path, ".worktrees/wip/services/ghost/app.py")

    report = scan_repo(tmp_path)
    assert _roots(report) == {"services/alpha"}
    assert "ghost" not in report.draft_yaml


def test_nested_service_inside_service_is_kept(tmp_path: Path) -> None:
    _spec(tmp_path, "services/outer/swagger.json", "Outer API")
    _code(tmp_path, "services/outer/app.py", "app = FastAPI()\n")
    _spec(tmp_path, "services/outer/sub/inner/swagger.json", "Inner API")
    _code(tmp_path, "services/outer/sub/inner/app.py", "app = FastAPI()\n")
    (tmp_path / "services/outer/sub/inner/pyproject.toml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "services/outer/sub/inner/pyproject.toml").write_text(
        "[project]\n", encoding="utf-8"
    )

    roots = _roots(scan_repo(tmp_path))
    assert "services/outer" in roots
    assert "services/outer/sub/inner" in roots


def test_duplicate_spec_in_client_dir_is_not_a_service(tmp_path: Path) -> None:
    _spec(tmp_path, "services/alpha/swagger.json", "Alpha API")
    _code(tmp_path, "services/alpha/app.py")
    # Identical copy vendored into a generated client.
    dup = tmp_path / "clients/alpha/generated/swagger.json"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text((tmp_path / "services/alpha/swagger.json").read_text(), encoding="utf-8")
    _code(tmp_path, "clients/alpha/generated/client.py")

    report = scan_repo(tmp_path)
    assert _roots(report) == {"services/alpha"}
    assert any("clients/alpha" in a for a in report.ambiguities)


def test_generated_client_draft_carries_settings_factories(tmp_path: Path) -> None:
    _spec(tmp_path, "services/orders/swagger.json", "Orders API")
    _code(tmp_path, "services/orders/app.py")
    _code(
        tmp_path,
        "services/orders/views.py",
        "import settings\n\ndef view():\n"
        "    api = settings.GET_PAYMENTS_API()\n"
        "    return get_V1Api(paymentscli, 'PAYMENTS_ENDPOINT')\n",
    )
    _code(tmp_path, "clients/paymentscli/api.py", "# Swagger Codegen generated\n")

    report = scan_repo(tmp_path)
    clients = [c for s in report.services for c in s["clients"]]
    assert len(clients) == 1
    assert clients[0]["factories"] == ["settings.GET_PAYMENTS_API"]
    assert clients[0]["packages"] == ["paymentscli"]
    assert any("drafted factories" in a for a in report.ambiguities)


def test_build_file_at_repo_root_does_not_crash_scan(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='mono'\n", encoding="utf-8")
    _spec(tmp_path, "services/alpha/swagger.json", "Alpha API")
    _code(tmp_path, "services/alpha/app.py")

    assert _roots(scan_repo(tmp_path)) == {"services/alpha"}


def test_fixture_apps_do_not_become_services(tmp_path: Path) -> None:
    _spec(tmp_path, "services/alpha/swagger.json", "Alpha API")
    _code(tmp_path, "services/alpha/app.py")
    _code(tmp_path, "tests/fixtures/fake-app/main.go", "package main\nfunc main() {}\n")

    assert _roots(scan_repo(tmp_path)) == {"services/alpha"}


def test_spec_title_names_the_service(tmp_path: Path) -> None:
    _spec(tmp_path, "services/billing-svc/swagger.json", "Billing API")
    _code(tmp_path, "services/billing-svc/app.py")

    report = scan_repo(tmp_path)
    svc = report.services[0]
    assert svc["id"] == "billing-api"
    assert svc["specs"] == ["services/billing-svc/swagger.json"]
    assert report.evidence["billing-api"]
