import pytest
from pydantic import ValidationError

from axon.v1.servicemap import ServiceMap, load_service_map


def test_keyed_corrections_and_tombstones(tmp_path):
    (tmp_path / "base.yaml").write_text("""services:
- id: app
  clients: [{id: sdk, packages: [sdk]}]
  links: [{id: redirect, call_name: charge, to_service: ledger, to_path: /old}]
- id: removed
links: [{id: root-link, to_service: ledger, to_path: /old}]
""")
    (tmp_path / "axon.yaml").write_text("""extends: base.yaml
services:
- {id: removed, +delete: true}
- id: app
  clients: [{id: sdk, +delete: true}]
  links: [{id: redirect, to_path: /new}]
links: [{id: root-link, +delete: true}]
""")
    sm = load_service_map(tmp_path)
    assert [s.id for s in sm.services] == ["app"]
    assert sm.services[0].clients == []
    assert sm.services[0].links[0].to_path == "/new"
    assert sm.services[0].links[0].call_name == "charge"
    assert sm.links == []


def test_client_ids_are_repo_global():
    with pytest.raises(ValidationError, match="duplicate client id"):
        ServiceMap.model_validate(
            {
                "services": [
                    {"id": "a", "clients": [{"id": "sdk"}]},
                    {"id": "b", "clients": [{"id": "sdk"}]},
                ]
            }
        )


@pytest.mark.parametrize(
    "evidence", [{}, {"file": "x", "line": 1}, {"file": "x", "line": 0, "text": "x"}]
)
def test_declared_calls_require_complete_evidence(evidence):
    with pytest.raises(ValidationError):
        ServiceMap.model_validate(
            {
                "services": [
                    {
                        "id": "app",
                        "calls": [
                            {
                                "id": "manual",
                                "from_api": "POST /charge",
                                "to": {"service": "ledger", "method": "POST", "path": "/entries"},
                                "evidence": evidence,
                            }
                        ],
                    }
                ]
            }
        )


def test_team_overlay_glob_sorted_and_scoped(tmp_path):
    directory = tmp_path / "services/app"
    directory.mkdir(parents=True)
    (tmp_path / "axon.yaml").write_text("""services: [{id: app, roots: [services/app]}]
overrides: ["services/**/axon.service.yaml"]
""")
    overlay = directory / "axon.service.yaml"
    overlay.write_text("services: [{id: app, name: Team App}]\n")
    assert load_service_map(tmp_path).services[0].name == "Team App"
    overlay.write_text("services: [{id: intruder, roots: [elsewhere]}]\n")
    with pytest.raises(ValueError, match="overlay-scope"):
        load_service_map(tmp_path)


def test_legacy_unkeyed_links_replace(tmp_path):
    (tmp_path / "base.yaml").write_text("links: [{to_service: a, to_path: /a}]\n")
    (tmp_path / "axon.yaml").write_text(
        "extends: base.yaml\nlinks: [{to_service: b, to_path: /b}]\n"
    )
    assert [link.to_service for link in load_service_map(tmp_path).links] == ["b"]
