"""Generated client metadata never establishes service ownership."""

import pytest

from axon.v2.servicemap import ClientDecl, ServiceDecl, ServiceMap, owner_of

pytestmark = pytest.mark.tier1


def test_cfg_and_build_metadata_add_clients_only_to_declared_owner(tmp_path):
    from axon.v2.client_discovery import discover_clients

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
    from axon.v2.client_discovery import discover_clients

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
    from axon.v2.client_discovery import discover_clients

    (tmp_path / "BUILD").write_text(
        'python_wheel_name="unrelated"\n'
        'library(name="lib", java_api_package="wrong.api")\n'
        'swagger_client(name="api", python_wheel_name=dynamic_name)\n'
    )
    sm = ServiceMap(services=[ServiceDecl(id="app", roots=["."])])
    assert discover_clients(tmp_path, sm, ["BUILD"]).services[0].clients == []


def test_onboarding_includes_generator_metadata_in_reviewable_draft(tmp_path):
    import yaml

    from axon.v2.onboard import scan_repo

    (tmp_path / "pyproject.toml").write_text('[project]\nname="sample"\n')
    (tmp_path / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "swagger.cfg").write_text('{"packageName":"samplecli"}')
    draft = yaml.safe_load(scan_repo(tmp_path).draft_yaml)
    assert draft["services"][0]["clients"][0]["packages"] == ["samplecli"]
    assert draft["services"][0]["clients"][0]["target_service"] == draft["services"][0]["id"]
