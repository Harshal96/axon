package generated.api;
// OpenAPI Generator
class ItemsApi { public Item readWithHttpInfo() { String localVarPath = UriComponentsBuilder.fromPath("/v2/items/{id}").toUriString(); apiClient.invokeAPI(localVarPath, HttpMethod.GET, args); return null; } }
