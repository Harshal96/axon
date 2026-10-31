@RequestMapping(produces="application/json", path="/items")
public class Resource {
    @Operation(operationId="readItems")
    @GetMapping(produces="application/json", value="/{id}")
    public ResponseEntity<
        List<Item>> read(String id) { return null; }
}
