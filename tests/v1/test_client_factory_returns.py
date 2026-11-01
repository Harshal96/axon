from pathlib import Path

from axon.v1.ask import ask_deterministic
from axon.v1.facts import facts_from_dict, facts_to_dict
from axon.v1.indexer import index_repo
from axon.v1.models import EdgeKind, NodeKind
from axon.v1.onboard_suggestions import client_factory_suggestions
from axon.v1.parse_python import parse_python
from axon.v1.servicemap import load_service_map

FIXTURE = Path(__file__).parents[1] / "fixtures/evals/python-settings-client-factory"


def test_factory_returns_survive_cache_with_return_evidence():
    facts = parse_python(
        "settings.py",
        "import generated_sdk\ndef client_factory():\n    return generated_sdk.V1Api()\n",
    )
    assert len(facts.factory_returns) == 1
    assert facts.factory_returns[0].line == 3
    assert facts_from_dict(facts_to_dict(facts)) == facts


def test_inferred_return_evidence_and_dynamic_blind_spot():
    snap, _ = index_repo(str(FIXTURE))
    response = ask_deterministic(snap, "which apis in consumer service call directory")
    assert response is not None and len(response.rows) == 6
    assert any("Unlinked client evidence" in spot for spot in response.blind_spots)
    inferred = [r for r in response.rows if r.rule == "factory:inferred-return"]
    assert len(inferred) == 4
    assert all(r.via.file == "consumer/views.py" and r.via.line for r in inferred)
    assert any(
        "consumer/settings.py:5 return directory_sdk" in e.evidence.detail
        for e in snap.edges
        if e.kind == EdgeKind.CALLS_API
    )
    assert any("factory" in d.lower() and "package" in d for d in snap.diagnostics)
    dynamic = next(n for n in snap.by_kind(NodeKind.API_ENDPOINT) if n.props["path"] == "/dynamic")
    assert not snap.outbound_calls(dynamic.id)


def test_settings_ambiguity_is_diagnostic_and_never_crosses_service(tmp_path):
    (tmp_path / "axon.yaml").write_text(
        "version: 1\nrepo: sample\nservices:\n  - id: consumer\n"
        "    roots: [consumer]\n    language: python\n"
    )
    (tmp_path / "consumer/one").mkdir(parents=True)
    (tmp_path / "consumer/two").mkdir(parents=True)
    for directory in ["one", "two"]:
        (tmp_path / f"consumer/{directory}/settings.py").write_text(
            "def factory():\n    return object()\n"
        )
    (tmp_path / "consumer/views.py").write_text(
        "from django.conf import settings\ndef view():\n    return settings.factory()\n"
    )
    snap, _ = index_repo(str(tmp_path))
    assert any("django-settings:ambiguous" in d for d in snap.diagnostics)


def test_onboarding_suggests_only_missing_declared_factory():
    sm = load_service_map(str(FIXTURE))
    notes = client_factory_suggestions(FIXTURE, sm, ["consumer/views.py"])
    assert any("settings.GET_DIRECTORY_API" in note for note in notes)
    sm.services[0].clients[0].factories = ["settings.GET_DIRECTORY_API"]
    assert not client_factory_suggestions(FIXTURE, sm, ["consumer/views.py"])


def test_unique_settings_fallback_in_service_root():
    from axon.v1.indexer import _CallResolver

    settings = parse_python("settings.py", "def get_client():\n    return object()\n")
    views = parse_python(
        "views.py",
        "from django.conf import settings\ndef handler():\n    return settings.get_client()\n",
    )
    resolver = _CallResolver(
        [settings, views],
        {"settings.py": ("service", "consumer"), "views.py": ("service", "consumer")},
    )
    assert resolver.resolve("settings.get_client", "views.py", "handler") == [
        ("settings.py", "get_client")
    ]
