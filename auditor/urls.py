from django.urls import path

from . import views

app_name = "auditor"

urlpatterns = [
    path("", views.index, name="index"),
    path("audits/<int:pk>/", views.audit_detail, name="detail"),
]
