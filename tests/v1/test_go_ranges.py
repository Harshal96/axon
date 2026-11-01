from axon.v1.parse_go import parse_go


def test_go_ranges_do_not_assign_top_level_calls_to_previous_function():
    facts = parse_go(
        "main.go",
        """package app
func first() {
    task := func() { client.first() }
    task()
}
var outside = client.initialize()
func second() {
    client.second()
}
// func fake() { client.fake() }
var data = "client.fake()"
""",
    )
    assert [(s.qualified, s.line_end) for s in facts.symbols] == [("first", 5), ("second", 9)]
    callers = {c.called: c.caller for c in facts.calls}
    assert callers["client.first"] == "first"
    assert callers["client.second"] == "second"
    assert "client.initialize" not in callers
    assert "client.fake" not in callers
    assert "first" not in callers


def test_handle_registrations_are_not_duplicated():
    facts = parse_go(
        "main.go",
        """package main

func main() {
    mux := http.NewServeMux()


    mux.HandleFunc("/healthz", health)
    mux.Handle("/metrics", metrics)
    mux.Handle("/metrics", metrics)
}
""",
    )
    assert [(r.method, r.path, r.line) for r in facts.routes] == [
        ("ANY", "/healthz", 7),
        ("ANY", "/metrics", 8),
        ("ANY", "/metrics", 9),
    ]
