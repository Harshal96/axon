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


def test_internal_routes_collapse_and_libraries_are_shared(tmp_path: Path) -> None:
    import yaml

    _code(tmp_path, "platform/pyproject.toml", "[project]\n")
    for name in ("alpha", "beta"):
        _code(tmp_path, f"platform/{name}/pyproject.toml", "[project]\n")
        _code(tmp_path, f"platform/{name}/main.py", "app = FastAPI()\n")
        _code(
            tmp_path,
            f"platform/{name}/routes/v1/area/main.py",
            "router = APIRouter()\n@router.get('/items')\ndef items(): pass\n",
        )
    _code(tmp_path, "libraries/common/pyproject.toml", "[project]\n")
    _code(tmp_path, "libraries/common/helpers.py", "def helper(): pass\n")
    report = scan_repo(tmp_path)
    assert _roots(report) == {"platform/alpha", "platform/beta"}
    assert yaml.safe_load(report.draft_yaml)["shared"] == ["libraries/common"]
    assert len(report.ambiguities) <= 4


def test_default_spec_titles_and_collisions_use_folder_names(tmp_path: Path) -> None:
    _spec(tmp_path, "apps/alpha/openapi.yaml", "FastAPI")
    _spec(tmp_path, "apps/beta/openapi.yaml", "OpenAPI definition")
    _spec(tmp_path, "east/service/openapi.yaml", "Custom API")
    _spec(tmp_path, "west/service/openapi.yaml", "Custom API")
    # Distinct documents can share a title without being duplicate copies.
    _code(
        tmp_path,
        "west/service/openapi.yaml",
        '{"openapi":"3.0.0","info":{"title":"Custom API","version":"2"},"paths":{}}',
    )
    report = scan_repo(tmp_path)
    ids = {service["roots"][0]: service["id"] for service in report.services}
    assert ids["apps/alpha"] == "alpha"
    assert ids["apps/beta"] == "beta"
    assert ids["east/service"] == "east-custom-api"
    assert ids["west/service"] == "west-custom-api"


def test_refresh_preserves_curation_and_ignores_with_reviewable_diff(tmp_path: Path) -> None:
    import yaml

    from axon.v2.onboard import refresh_repo

    _code(tmp_path, "apps/alpha/main.py", "app = FastAPI()\n")
    _code(tmp_path, "apps/beta/main.py", "app = FastAPI()\n")
    _code(tmp_path, "scripts/server.py", "app = FastAPI()\n")
    config = """repo: sample
ignore: [scripts]
shared: [libraries]
services:
  - id: curated
    name: Curated App
    roots: [apps/alpha]
    base_urls: [env:REMOTE_URL]
links: [{call_name: local, to_service: curated, to_path: /items}]
"""
    (tmp_path / "axon.yaml").write_text(config)
    report = refresh_repo(tmp_path)
    data = yaml.safe_load(report.draft_yaml)
    assert data["ignore"] == ["scripts"]
    assert data["shared"] == ["libraries"]
    assert data["links"][0]["call_name"] == "local"
    assert data["services"][0]["id"] == "curated"
    assert data["services"][0]["name"] == "Curated App"
    assert data["services"][0]["base_urls"] == ["env:REMOTE_URL"]
    assert {s["roots"][0] for s in data["services"]} == {"apps/alpha", "apps/beta"}
    assert "+" in report.diff and "beta" in report.diff
    assert (tmp_path / "axon.yaml").read_text() == config


def test_router_only_subpackages_never_override_parent_boundary(tmp_path: Path) -> None:
    _code(tmp_path, "apps/alpha/pyproject.toml", "[project]\n")
    _code(tmp_path, "apps/alpha/main.py", "app = FastAPI()\n")
    _code(tmp_path, "apps/alpha/routes/v1/area/pyproject.toml", "[project]\n")
    _code(tmp_path, "apps/alpha/routes/v1/area/router.py", "router = APIRouter()\n")
    assert _roots(scan_repo(tmp_path)) == {"apps/alpha"}


def test_bare_library_modules_propose_shared_roots(tmp_path: Path) -> None:
    import yaml

    _code(tmp_path, "apps/alpha/main.py", "app = FastAPI()\n")
    _code(tmp_path, "libraries/common/tools.py", "def utility(): pass\n")
    report = scan_repo(tmp_path)
    assert _roots(report) == {"apps/alpha"}
    assert yaml.safe_load(report.draft_yaml)["shared"] == ["libraries/common"]


def test_path_base_suggestions_include_counts_sites_and_do_not_mutate(tmp_path: Path) -> None:
    from axon.v2.onboard_suggestions import path_base_url_suggestions
    from axon.v2.servicemap import ServiceDecl, ServiceMap

    _code(
        tmp_path,
        "apps/alpha/main.py",
        "import os\nimport requests\n"
        "def run():\n    requests.get(f'{os.environ[\"REMOTE_URL\"]}/items')\n",
    )
    _code(
        tmp_path,
        "apps/beta/main.py",
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/items')\ndef items(): pass\n",
    )
    sm = ServiceMap(
        repo="sample",
        services=[
            ServiceDecl(id="alpha", roots=["apps/alpha"]),
            ServiceDecl(id="beta", roots=["apps/beta"]),
        ],
    )
    notes = path_base_url_suggestions(tmp_path, sm, ["apps/alpha/main.py", "apps/beta/main.py"])
    assert len(notes) == 1
    assert "beta base_urls: [env:REMOTE_URL]" in notes[0]
    assert "1 call site(s)" in notes[0] and "apps/alpha/main.py:4" in notes[0]
    assert not sm.services[1].base_urls


def test_refresh_preserves_glob_owned_service_without_duplicate_proposal(tmp_path: Path) -> None:
    import yaml

    from axon.v2.onboard import refresh_repo

    _code(tmp_path, "services/alpha/main.py", "app = FastAPI()\n")
    _code(tmp_path, "services/beta/main.py", "app = FastAPI()\n")
    (tmp_path / "axon.yaml").write_text(
        "repo: sample\nservices: [{id: curated, name: Curated, roots: ['services/*']}]\n"
    )
    report = refresh_repo(tmp_path)
    services = yaml.safe_load(report.draft_yaml)["services"]
    assert len(services) == 1
    assert services[0]["id"] == "curated"
    assert services[0]["roots"] == ["services/*"]
    assert report.diff == ""
    assert not any("Proposed addition" in note for note in report.notes)
