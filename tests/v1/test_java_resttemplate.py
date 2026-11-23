from axon.v1.parse_java import parse_java


def test_resttemplate_reads_complete_url_and_http_method_arguments():
    facts = parse_java(
        "Service.java",
        """import org.springframework.web.client.RestTemplate;
class Service {
    private RestTemplate transport;
    void send(String id, HttpMethod verb) {
        transport.exchange("/orders", HttpMethod.DELETE, null, String.class);
        transport.execute("/orders", verb, null, null);
        transport.getForObject("/orders/" + id, String.class);
        transport.getForObject("/orders" + "/latest", String.class);
    }
}""",
    )
    assert [(c.line, c.method, c.url) for c in facts.http_calls] == [
        (5, "DELETE", "/orders"),
        (6, "", "/orders"),
        (7, "GET", "/{dynamic}"),
        (8, "GET", "/orders/latest"),
    ]


def test_resttemplate_delete_requires_a_spring_receiver_in_scope():
    facts = parse_java(
        "Service.java",
        """import org.springframework.web.client.RestTemplate;
class Service {
    private RestTemplate transport;
    void deleteRemote(String id) {
        this.transport.delete("/orders/{id}", id);
    }
    void deleteLocal(Cache transport) {
        transport.delete("/cache");
        repository.delete("/cache");
    }
}""",
    )
    assert [(c.caller, c.line, c.method, c.url) for c in facts.http_calls] == [
        ("Service.deleteRemote", 5, "DELETE", "/orders/{id}"),
    ]
