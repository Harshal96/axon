package example.catalog;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
@Path("/items")
public class Resource {
    @GET @Path("/{id}")
    public Item read(String id) { return null; }
    @POST
    public Item add(Item item) { return item; }
}
