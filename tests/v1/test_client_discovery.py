"""Generated client metadata never establishes service ownership."""

import pytest

from axon.v1.servicemap import ClientDecl, ServiceDecl, ServiceMap, owner_of

pytestmark = pytest.mark.tier1


def test_cfg_and_build_metadata_add_clients_only_to_declared_owner(tmp_path):
    from axon.v1.client_discovery import discover_clients

    files = {
        "services/catalog/swagger.cfg": '{"packageName": "catalogcli"}',
        "services/catalog/BUILD": (
            'generated_swagger_client(name="api", python_wheel_name="catalogcli", '
            'java_api_package="com.example.catalog.api")'
        ),
        "services/catalog/catalogcli/api/items.py": "",
        "loose/swagger.cfg": '{"packageName": "unknowncli"}',
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    sm = ServiceMap(services=[ServiceDecl(id="catalog", roots=["services/catalog"])])
    discovered = discover_clients(tmp_path, sm, list(files))
    assert sm.services[0].clients == []
    clients = discovered.services[0].clients
    assert len(clients) == 1
    assert set(clients[0].packages) == {"catalogcli", "com.example.catalog.api"}
    assert clients[0].target_service == "catalog"
    assert owner_of(discovered, "services/catalog/catalogcli/api/items.py")[0] == "client"
    assert "unknowncli" not in clients[0].packages


def test_ambiguous_and_explicit_package_targets_are_not_overwritten(tmp_path):
    from axon.v1.client_discovery import discover_clients

    files = ["one/swagger.cfg", "two/swagger.cfg"]
    for name in files:
        path = tmp_path / name
        path.parent.mkdir()
        path.write_text('{"packageName": "commoncli", "apiPackage": "special.api"}')
    sm = ServiceMap(
        services=[
            ServiceDecl(
                id="one",
                roots=["one"],
                clients=[ClientDecl(id="explicit", packages=["special.api"], target_service="two")],
            ),
            ServiceDecl(id="two", roots=["two"]),
        ]
    )
    result = discover_clients(tmp_path, sm, files)
    assert result.services[0].clients == sm.services[0].clients
    assert result.services[1].clients == []


def test_build_metadata_must_be_in_client_rule_and_literal(tmp_path):
    from axon.v1.client_discovery import discover_clients

    (tmp_path / "BUILD").write_text(
        'python_wheel_name="unrelated"\n'
        'library(name="lib", java_api_package="wrong.api")\n'
        'swagger_client(name="api", python_wheel_name=dynamic_name)\n'
    )
    sm = ServiceMap(services=[ServiceDecl(id="app", roots=["."])])
    assert discover_clients(tmp_path, sm, ["BUILD"]).services[0].clients == []


def test_onboarding_includes_generator_metadata_in_reviewable_draft(tmp_path):
    import yaml

    from axon.v1.onboard import scan_repo

    (tmp_path / "pyproject.toml").write_text('[project]\nname="sample"\n')
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "swagger.cfg").write_text('{"packageName":"samplecli"}')
    draft = yaml.safe_load(scan_repo(tmp_path).draft_yaml)
    assert draft["services"][0]["clients"][0]["packages"] == ["samplecli"]
    assert draft["services"][0]["clients"][0]["target_service"] == draft["services"][0]["id"]


def test_discovery_cache_tracks_metadata_manifest_config_and_caller_mutation(tmp_path, monkeypatch):
    import axon.v1.client_discovery as discovery

    metadata = tmp_path / "swagger.cfg"
    metadata.write_text('{"packageName":"first"}')
    sm = ServiceMap(services=[ServiceDecl(id="app", roots=["."])])
    calls = []
    original = discovery._packages

    def tracked(name, text):
        calls.append(text)
        return original(name, text)

    monkeypatch.setattr(discovery, "_packages", tracked)
    files = ["swagger.cfg"]
    first = discovery.discover_clients(tmp_path, sm, files)
    first.services[0].clients.clear()
    assert discovery.discover_clients(tmp_path, sm, files).services[0].clients
    assert len(calls) == 1
    metadata.write_text('{"packageName":"other"}')
    assert discovery.discover_clients(tmp_path, sm, files).services[0].clients[0].packages == [
        "other"
    ]
    generated = tmp_path / "other" / "api.py"
    generated.parent.mkdir()
    generated.write_text("")
    files.append("other/api.py")
    assert discovery.discover_clients(tmp_path, sm, files).services[0].clients[0].paths == ["other"]
    generated.unlink()
    files.remove("other/api.py")
    assert discovery.discover_clients(tmp_path, sm, files).services[0].clients[0].paths == []
    sm.services[0].specs = ["spec.yaml"]
    assert (
        discovery.discover_clients(tmp_path, sm, files).services[0].clients[0].spec == "spec.yaml"
    )
    assert len(calls) == 4


def test_declared_empty_paths_located_repo_wide_only_with_generated_evidence(tmp_path):
    from axon.v1.client_discovery import discover_clients

    files = {
        "vendor_code/samplecli/api_client.py": "class ApiClient: pass",
        "vendor_code/samplecli/api/items.py": "",
        "app/ordinary/api.py": "class Ordinary: pass",
    }
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sm = ServiceMap(
        services=[
            ServiceDecl(
                id="app",
                roots=["app"],
                clients=[
                    ClientDecl(
                        id="remote", packages=["samplecli", "ordinary"], target_service="remote"
                    )
                ],
            )
        ]
    )
    result = discover_clients(tmp_path, sm, list(files))
    assert result.services[0].clients[0].paths == ["vendor_code/samplecli"]
    assert sm.services[0].clients[0].paths == []
    assert owner_of(result, "vendor_code/samplecli/api/items.py")[0] == "client"
