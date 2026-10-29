// Eval fixture: Spring ledger endpoints (parser input only).
package ledger;

@RestController
@RequestMapping("/v1")
public class LedgerController {

    @PostMapping("/entries")
    public Object createEntry(Object body) {
        return body;
    }

    @DeleteMapping("/entries/{id}")
    public Object deleteEntry(String id) {
        return id;
    }
}
