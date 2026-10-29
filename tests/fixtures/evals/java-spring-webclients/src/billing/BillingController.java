// Eval fixture: Spring verbs + RestTemplate/WebClient (parser input only).
package billing;

@RestController
@RequestMapping(path = "/v1/billing")
public class BillingController {
    private RestTemplate restTemplate;
    private WebClient webClient;

    @PutMapping("/charges/{id}")
    public Object replaceCharge(String id, Object body) {
        return saveCharge(body);
    }

    @RequestMapping(value = "/charges/{id}/refund", method = RequestMethod.PATCH)
    public Object refundCharge(String id) {
        return reverseEntry(id);
    }

    @DeleteMapping("/charges/{id}")
    public Object deleteCharge(String id) {
        return id;
    }

    public Object saveCharge(Object body) {
        return restTemplate.postForObject("http://ledger/v1/entries", body, Object.class);
    }

    public Object reverseEntry(String id) {
        return webClient.delete().uri("http://ledger/v1/entries/{id}", id).retrieve();
    }
}
