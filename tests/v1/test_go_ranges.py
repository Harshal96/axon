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


def test_net_http_request_constructors_read_the_method_and_url_arguments():
    facts = parse_go(
        "client.go",
        """package client
import "net/http"
func send() {
    http.NewRequestWithContext(
        context.WithValue(ctx, key, "a,b"), http.MethodPatch, "https://example.test/orders", nil,
    )
    http.NewRequest("POST", `https://example.test/items`, nil)
}
""",
    )
    assert [(c.caller, c.line, c.call_name, c.method, c.url) for c in facts.http_calls] == [
        ("send", 4, "http.NewRequestWithContext", "PATCH", "https://example.test/orders"),
        ("send", 7, "http.NewRequest", "POST", "https://example.test/items"),
    ]


def test_dynamic_go_requests_remain_visible_without_a_fabricated_destination():
    facts = parse_go(
        "client.go",
        """package client
import "net/http"
func send(path, id string) {
    http.Get("https://example.test/orders/" + id)
    http.NewRequest("PUT", buildURL(path, id), nil)
    http.Head(path)
    http.Post("https://example.test/status", "text/plain", nil)
    // http.Get("/comment")
    _ = `http.Get("/example")`
}
""",
    )
    assert [(c.line, c.method, c.url) for c in facts.http_calls] == [
        (4, "GET", "/{dynamic}"),
        (5, "PUT", "/{dynamic}"),
        (6, "HEAD", "/{dynamic}"),
        (7, "POST", "https://example.test/status"),
    ]


def test_go_outbound_receiver_is_the_net_http_package_binding():
    facts = parse_go(
        "client.go",
        """package client
import (
    web "net/http"
    http "example.test/cache"
)
func send(path string) {
    http.Get(path)
    web.NewRequest(web.MethodDelete, "/orders", nil)
}
""",
    )
    assert [(c.call_name, c.method, c.url) for c in facts.http_calls] == [
        ("web.NewRequest", "DELETE", "/orders"),
    ]
