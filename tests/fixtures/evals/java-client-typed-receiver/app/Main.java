import generated_sdk.api.V1Api;
class Main {
 @GetMapping("/local")
 public void local(V1Api api, String generated_sdk) {
  api.list_items();
  api.toLowerCase();
  api.getId();
  generated_sdk.list_items();
 }
}
