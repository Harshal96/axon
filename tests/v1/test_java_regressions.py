import pytest

from axon.v1.parse_java import parse_java


def test_spring_multiline_generic_path_and_operation():
    facts = parse_java(
        "Resource.java",
        """
@RequestMapping(produces="application/json", path="/items")
public class Resource {
 @Operation(operationId="readItem")
 @GetMapping(produces="application/json", value="/{id}")
 public ResponseEntity<
     List<Item>> read(
     String id) { return service.get(id); }
}
""",
    )
    assert [(r.method, r.path, r.handler, r.operation_id) for r in facts.routes] == [
        ("GET", "/items/{id}", "Resource.read", "readItem")
    ]
    assert "service.get" in [c.called for c in facts.calls]
    assert "read" not in [c.called for c in facts.calls]


def test_jaxrs_prefix_verbs_and_same_line_annotations():
    facts = parse_java(
        "Resource.java",
        """
@Path("/items")
public class Resource {
 @GET @Path("/{id}")
 public Item read(String id) { return store.get(id); }
 @POST
 public Item add(Item item) { return store.update(item); }
}
""",
    )
    assert [r.line for r in facts.routes] == [4, 6]
    assert [(r.method, r.path, r.handler) for r in facts.routes] == [
        ("GET", "/items/{id}", "Resource.read"),
        ("POST", "/items", "Resource.add"),
    ]


def test_calls_preserve_chained_receivers_and_exclude_annotations_declarations():
    facts = parse_java(
        "Resource.java",
        """
import java.util.List;
public class Resource {
 private List<Item> values;
 @GetMapping("/items")
 public Item read() {
   Logger.info("read");
   values.get(0);
   Store.getInstance().update();
   apiFactory.client().fetchItems();
   return service.get();
 }
}
""",
    )
    called = [c.called for c in facts.calls]
    assert "Store.getInstance().update" in called
    assert "apiFactory.client().fetchItems" in called
    assert "service.get" in called
    assert "Logger.info" not in called
    assert "values.get" not in called
    assert "GetMapping" not in called
    assert "read" not in called


def test_java_resolution_metadata():
    facts = parse_java(
        "Resource.java",
        """
package example.catalog;
import example.client.CatalogApi;
public class Resource extends BaseResource {
 private CatalogApi api;
 public static Resource getInstance() { return new Resource(); }
 public CatalogApi client() { return api; }
 public Item read() { CatalogApi local = client(); return local.get(); }
}
""",
    )
    assert facts.package == "example.catalog"
    assert facts.import_map["CatalogApi"] == "example.client.CatalogApi"
    assert facts.superclasses == {"Resource": ["BaseResource"]}
    assert facts.returns["Resource.getInstance"] == "Resource"
    assert facts.returns["Resource.client"] == "CatalogApi"
    assert any(b.var == "api" and b.source == "CatalogApi" for b in facts.bindings)
    assert any(b.var == "local" and b.source == "client" for b in facts.bindings)


@pytest.mark.parametrize(
    "annotation",
    [
        '@Operation(operationId="readItem")',
        '@ApiOperation(value="Read item", nickname="readItem")',
    ],
)
@pytest.mark.parametrize("first", [True, False])
def test_spring_operation_annotation_order(annotation, first):
    mapping = '@GetMapping("/items")'
    annotations = f"{annotation}\n{mapping}" if first else f"{mapping}\n{annotation}"
    facts = parse_java(
        "Resource.java",
        f"public class Resource {{\n{annotations}\npublic Item read() {{ return null; }}\n}}",
    )
    assert facts.routes[0].operation_id == "readItem"


def test_java_balanced_signatures_ranges_and_nested_ownership():
    text = """public class Resource {
 @GetMapping("/first")
 public Map<String, List<Item>> first(
     @RequestParam(defaultValue = ")") String value,
     @Valid(groups = {First.class, Second.class}) Item item)
     throws IOException, IllegalStateException {
   Runnable task = () -> { client.first(); };
   return service.first();
 }
 @GetMapping("/second")
 public Item second(@RequestParam(required = false) String value) {
   return service.second();
 }
 class Inner {
   @GetMapping("/nested")
   public Item nested() { return service.nested(); }
 }
 private Worker worker = new Worker() {
   public void run() { service.anonymous(); }
 };
 private Item outside = service.initialize();
}"""
    facts = parse_java("Resource.java", text)
    symbols = {s.qualified: s for s in facts.symbols}
    assert [(r.path, r.handler) for r in facts.routes] == [
        ("/first", "Resource.first"),
        ("/second", "Resource.second"),
        ("/nested", "Resource.Inner.nested"),
    ]
    assert symbols["Resource.first"].line_end == 9
    assert symbols["Resource.second"].line_end == 13
    assert symbols["Resource"].line_end == 22
    callers = {c.called: c.caller for c in facts.calls}
    assert callers["client.first"] == "Resource.first"
    assert callers["service.first"] == "Resource.first"
    assert callers["service.second"] == "Resource.second"
    assert callers["service.nested"] == "Resource.Inner.nested"
    assert callers["service.anonymous"].startswith("Resource.Anonymous")
    assert callers["service.anonymous"].endswith(".run")
    assert callers["service.initialize"] == "Resource"


def test_java_mapping_does_not_skip_a_field_to_attach_next_method():
    facts = parse_java(
        "Resource.java",
        """public class Resource {
 @GetMapping("/wrong")
 private String value;
 public void next() { service.call(); }
}""",
    )
    assert facts.routes == []
    assert any(d.category == "suspect-handler" for d in facts.diagnostics)


def test_java_comments_literals_and_interface_extents():
    facts = parse_java(
        "Resource.java",
        """public interface Resource {
 @GetMapping("/item")
 Item read(@RequestParam(defaultValue = ")") String value);
 // public void fake() { service.fake(); }
 String DATA = "public void fake() { service.fake(); }";
}""",
    )
    assert [(r.handler, r.path) for r in facts.routes] == [("Resource.read", "/item")]
    assert [(s.qualified, s.line_start, s.line_end) for s in facts.symbols] == [
        ("Resource", 1, 6),
        ("Resource.read", 3, 3),
    ]
    assert facts.calls == []
