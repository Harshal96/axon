// Fixture Vert.x verticle (parser input only, never compiled).
package gateway;

public class MainVerticle {
    private WebClient client;

    public void startHttp(Router router) {
        router.post("/v1/charges").handler(this::createCharge);
        router.get("/health").handler(this::health);
    }

    public void createCharge(Object ctx) {
        checkLedger(ctx);
    }

    public void checkLedger(Object ctx) {
        client.get(8080, "ledger", "/v1/entries").send();
    }

    public void health(Object ctx) {
    }
}
