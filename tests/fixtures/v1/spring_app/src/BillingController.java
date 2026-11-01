// Fixture Spring controller (parser input only, never compiled).
package billing;

@RestController
@RequestMapping("/v1")
public class BillingController {
    private RestTemplate restTemplate;

    @PostMapping("/charges")
    public Object createCharge(Object body) {
        return chargeService(body);
    }

    @GetMapping("/charges/{id}")
    public Object getCharge(String id) {
        return loadCharge(id);
    }

    public Object chargeService(Object body) {
        return restTemplate.postForObject("http://ledger/v1/entries", body, Object.class);
    }

    public Object loadCharge(String id) {
        return id;
    }
}
