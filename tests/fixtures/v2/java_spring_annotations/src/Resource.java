@RequestMapping(produces="application/json", path="/items")
public class Resource {
    @Operation(operationId="readItems")
    @GetMapping(produces="application/json", value="/{id}")
    public ResponseEntity<
        List<Item>> read(@RequestParam(defaultValue = ")") String id)
        throws IOException {
        return lookup(id);
    }

    @GetMapping("/status")
    public Item status(@RequestParam(required = false) String value) {
        return null;
    }

    private Item lookup(String id) {
        return client.getForObject("http://catalog/items/status", Item.class);
    }
}
