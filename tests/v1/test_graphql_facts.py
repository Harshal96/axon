from axon.v1.facts import facts_from_dict, facts_to_dict


def test_graphql_facts_codec_keeps_embedded_and_standalone_inputs():
    data = {
        "path": "queries.gql",
        "language": "graphql",
        "graphql_document": {"path": "queries.gql", "text": "{ orders }", "line": 1, "column": 1},
        "apollo": {"exports": {"query": "GetOrders"}},
    }
    encoded = facts_to_dict(facts_from_dict(data))
    assert encoded["graphql_document"]["text"] == "{ orders }"
    assert encoded["apollo"] == data["apollo"]
