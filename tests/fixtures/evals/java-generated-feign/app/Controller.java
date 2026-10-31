import generated.api.ItemsApi;
class Controller {
 private ItemsApi api;
 @GetMapping("/local") public void local() { api.readWithHttpInfo(); }
}
