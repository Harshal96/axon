from rest_framework.routers import SimpleRouter
from rest_framework.viewsets import ViewSet
from rest_framework.decorators import action
class Items(ViewSet):
    def list(self): pass
    def retrieve(self): pass
    @action(detail=True, methods=['post'])
    def archive(self): pass
router = SimpleRouter()
router.register('items', Items)
urlpatterns = router.urls
urlpatterns += router.urls
urlpatterns.extend(router.urls)
urlpatterns = [*urlpatterns, *router.urls]
