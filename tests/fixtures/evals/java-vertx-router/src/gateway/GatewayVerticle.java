// Eval fixture: Vert.x router verbs + WebClient (parser input only).
package gateway;

public class GatewayVerticle {
    private WebClient client;

    public void startHttp(Router router) {
        router.put("/v1/charges/:id").handler(this::replaceCharge);
        router.delete("/v1/charges/:id").handler(this::deleteCharge);
        router.get("/health").handler(this::health);
    }

    public void replaceCharge(Object ctx) {
        forwardEntry(ctx);
    }

    public void deleteCharge(Object ctx) {
    }

    public void health(Object ctx) {
    }

    public void forwardEntry(Object ctx) {
        client.put(8080, "ledger", "/v1/entries").send();
    }
}
