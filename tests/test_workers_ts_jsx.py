from axon.workers.ts.jsx import extract_jsx_call_edges


def test_extract_jsx_call_edges():
    symbols = [
        {"id": "sym1", "jsx_tags_used": ["Button", "Input"]},
        {"id": "sym2", "jsx_tags_used": ["Unknown"]},
    ]
    name_to_sym_ids = {"Button": ["btn_id"], "Input": ["inp_id1", "inp_id2"]}
    edges = extract_jsx_call_edges(symbols, name_to_sym_ids)
    assert len(edges) == 3
    # Check that sym1 calls btn_id, inp_id1, inp_id2
    edge_to = [e["to_id"] for e in edges if e["from_id"] == "sym1"]
    assert "btn_id" in edge_to
    assert "inp_id1" in edge_to
    assert "inp_id2" in edge_to
