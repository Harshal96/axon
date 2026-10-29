// Eval fixture: Vert.x ledger endpoint (parser input only).
package ledger;

public class LedgerVerticle {

    public void startHttp(Router router) {
        router.put("/v1/entries").handler(this::putEntry);
    }

    public void putEntry(Object ctx) {
    }
}
