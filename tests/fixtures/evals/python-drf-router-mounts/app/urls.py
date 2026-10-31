from django.urls import path, include
urlpatterns = [path('v2/', include('app.routes'))]
