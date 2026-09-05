from django.urls import path

from . import views

app_name = "auditor"

urlpatterns = [
    path("", views.index, name="index"),
    path("audits/<int:pk>/", views.audit_detail, name="detail"),
    # Odpytywany przez stronę szczegółów, dopóki audyt wykonuje się w tle.
    path("audits/<int:pk>/status/", views.audit_status, name="status"),
    path("audits/<int:audit_id>/pdf/", views.download_pdf_report, name="download_pdf_report"),
    path("audits/<int:pk>/ga4/connect/", views.start_ga4_auth, name="start_ga4_auth"),
    path("ga4/callback/", views.ga4_callback, name="ga4_callback"),
    path("audits/<int:pk>/ga4/select-property/", views.select_ga4_property, name="select_ga4_property"),
]
