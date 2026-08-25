from django.urls import path

from . import views

app_name = "auditor"

urlpatterns = [
    path("", views.index, name="index"),
    path("audits/<int:pk>/", views.audit_detail, name="detail"),
    path("audits/<int:audit_id>/pdf/", views.download_pdf_report, name="download_pdf_report"),
]
