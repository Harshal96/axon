from tools.benchmark_scale import generate_repo


def test_synthetic_repo_indexes_all_languages(tmp_path):
    from axon.v2.indexer import index_repo
    from axon.v2.models import NodeKind

    generate_repo(tmp_path, services=3, files=9, symbols_per_file=2)
    snap, service_map = index_repo(tmp_path, use_cache=False)
    assert len(service_map.services) == 3
    assert len(snap.by_kind(NodeKind.SYMBOL)) >= 18
    assert {node.props["language"] for node in snap.by_kind(NodeKind.SOURCE_FILE)} >= {
        "python",
        "java",
        "go",
    }


def test_scale_measurement_exercises_incremental_state(tmp_path):
    from tools.benchmark_scale import run_benchmark

    report = run_benchmark(tmp_path / "repo", services=3, files=12, symbols=2, store_kind="memory")
    assert report["unchanged"]["indexing"]["mode"] == "incremental"
    assert report["unchanged"]["indexing"]["delta_edges"] == 0
    assert report["edited_one_percent"]["indexing"]["state_persisted"]
    assert report["max_facts_blob_bytes"] < 64 * 1024 * 1024
    assert report["full"]["phase_s"]["state"] > 0
