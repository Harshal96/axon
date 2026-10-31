import static common.Paths.ROOT;
@RequestMapping(ROOT)
class Controller {
 static final String ITEM = "/items/";
 @GetMapping(path={ITEM + "{id}", "/jobs/" +
 "{id}"}) public void read() {}
 @PostMapping("/items/" + UNKNOWN + "/run") public void run() {}
}
