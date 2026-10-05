from django.urls import path

from . import views

app_name = "auditor"

urlpatterns = [
    # Hub narzędziowy - ekran wyboru narzędzia zaraz po zalogowaniu.
    path("", views.HubView.as_view(), name="hub"),

    # Synteza dla domeny wskazanej na hubie - bez wymaganego audytu technicznego.
    path("investigate/", views.investigate_domain_view, name="investigate_domain"),

    # Panel analityki: wybór audytu, którego dane GA4/GSC chcemy oglądać.
    # Dashboard analityki bieżącej domeny - bez pośredniej listy wyboru.
    path("analytics/", views.analytics_dashboard, name="analytics"),
    path("analytics/disconnect/", views.google_disconnect, name="google_disconnect"),
    path(
        "analytics/<int:pk>/assign/",
        views.assign_google_services,
        name="assign_google_services",
    ),

    # Skaner techniczny. Nazwa `index` zostaje, bo wskazuje na nią kilkanaście
    # miejsc w kodzie i szablonach - zmienia się tylko adres.
    path("audits/", views.index, name="index"),
    path("audits/<int:pk>/", views.audit_detail, name="detail"),
    # Analityka JEDNEGO audytu - dwa stany: podłącz albo pokaż dane.
    path("audits/<int:pk>/analytics/", views.audit_analytics, name="audit_analytics"),
    # Odpytywany przez stronę szczegółów, dopóki audyt wykonuje się w tle.
    path("audits/<int:pk>/status/", views.audit_status, name="status"),
    # Holistyczna synteza: agenci zbierają dane ze wszystkich dostępnych modułów.
    path("audits/<int:pk>/investigate/", views.audit_investigate_view, name="investigate"),
    # Dane GA4/GSC dla wybranego zakresu dat (AJAX z sekcji "Widoczność i Ruch").
    path("audits/<int:pk>/analytics-data/", views.analytics_data, name="analytics_data"),
    path("audits/<int:audit_id>/pdf/", views.download_pdf_report, name="download_pdf_report"),
    # Eksport raportu: plik do pobrania (?format=xlsx|csv) albo arkusz Google Sheets.
    path("audits/<int:pk>/export/", views.export_report, name="export_report"),
    path("audits/<int:pk>/export/sheets/", views.export_to_google_sheets, name="export_to_google_sheets"),
    # Podpowiedzi adresów szablonów z sitemap.xml (AJAX z formularza nowego audytu).
    path("sitemap-suggestions/", views.sitemap_suggestions, name="sitemap_suggestions"),

    # Widoczność w wyszukiwarkach AI (GEO Tracker) - trzecia sekcja nawigacji.
    path("geo-visibility/", views.GeoVisibilityDashboardView.as_view(), name="geo_dashboard"),
    path("geo-visibility/<int:pk>/", views.geo_study_detail, name="geo_detail"),
    # Odpytywany przez pasek postępu, dopóki badanie wykonuje się w tle.
    path("geo-visibility/<int:pk>/status/", views.geo_study_status, name="geo_status"),
    path("geo-visibility/<int:pk>/rerun/", views.geo_study_rerun, name="geo_rerun"),
    path("geo-visibility/suggest-questions/", views.geo_suggest_questions, name="geo_suggest_questions"),
    path("audits/<int:pk>/ga4/connect/", views.start_ga4_auth, name="start_ga4_auth"),
    path("ga4/callback/", views.ga4_callback, name="ga4_callback"),
    path("audits/<int:pk>/ga4/select-property/", views.select_ga4_property, name="select_ga4_property"),
]
