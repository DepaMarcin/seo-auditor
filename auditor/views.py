"""Widoki aplikacji audytora.

Odpowiadają wyłącznie za obsługę żądania HTTP: autoryzację, walidację wejścia i złożenie
kontekstu dla szablonu. Logika audytu mieszka w `auditor.services`, a etykiety i
przekształcenia metryk na struktury dla szablonów - w `auditor.presentation`.

Wszystkie widoki wymagają zalogowania i operują wyłącznie na audytach należących do
zalogowanego użytkownika (`Audit.owner`) - audyt zawiera dane analityczne firmy (GA4,
Search Console), więc znajomość samego identyfikatora nie może dawać do nich dostępu.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from google_auth_oauthlib.flow import Flow

from .models import Audit, AuditMetric
from .presentation import (
    MERGED_PAGESPEED_SCORE_KEYS,
    TEAM_BY_CATEGORY,
    annotate_metric_labels,
    build_schema_status_table,
    compute_category_scores,
    group_technical_accordions,
    priority_for_metric,
    score_bucket,
)
from .ratelimit import is_rate_limited
from .services.ga4_service import GA4OAuthService
from .services.url_guard import UnsafeUrlError, validate_public_url
from .tasks import enqueue_audit

logger = logging.getLogger(__name__)

# Liczba audytów na liście na stronie głównej.
RECENT_AUDITS_LIMIT = 10


def _get_owned_audit(request: HttpRequest, pk: int) -> Audit:
    """Pobiera audyt należący do zalogowanego użytkownika albo zwraca 404.

    Świadomie 404, a nie 403: brak audytu i brak uprawnień muszą wyglądać identycznie,
    żeby nie dało się przez kod odpowiedzi ustalić, które identyfikatory istnieją.
    """
    return get_object_or_404(Audit, pk=pk, owner=request.user)


@login_required
def index(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        if is_rate_limited(request, scope="audit"):
            messages.error(
                request,
                "Przekroczono limit uruchamianych audytów. Spróbuj ponownie za jakiś czas.",
            )
            return redirect("auditor:index")

        try:
            url = validate_public_url(request.POST.get("url", ""))
        except UnsafeUrlError as exc:
            messages.error(request, str(exc))
            return redirect("auditor:index")

        audit = Audit.objects.create(url=url, owner=request.user)
        # Audyt trwa minuty (PageSpeed + rekomendacje AI), więc leci w tle - strona
        # szczegółów odpytuje potem `audit_status` i odświeża się po zakończeniu.
        enqueue_audit(audit.pk)
        return redirect("auditor:detail", pk=audit.pk)

    audits = Audit.objects.filter(owner=request.user).order_by("-created_at")[:RECENT_AUDITS_LIMIT]
    return render(request, "auditor/index.html", {"audits": audits})


@login_required
def audit_status(request: HttpRequest, pk: int) -> JsonResponse:
    """Lekki endpoint JSON dla frontendu: stan audytu wykonywanego w tle."""
    audit = _get_owned_audit(request, pk)
    return JsonResponse({
        "status": audit.status,
        "status_label": audit.get_status_display(),
        "score": audit.score,
        "finished": audit.status in (Audit.Status.COMPLETED, Audit.Status.FAILED),
    })


# ----------------------------------------------------------------------
# Google Analytics 4 - integracja OAuth 2.0 ("Zaloguj się przez Google")
# ----------------------------------------------------------------------

@login_required
def start_ga4_auth(request: HttpRequest, pk: int) -> HttpResponse:
    """Inicjuje przepływ OAuth 2.0 z Google dla danego audytu: buduje `Flow` z pliku
    `client_secret.json`, zapisuje w sesji, którego audytu dotyczy autoryzacja
    (`pending_audit_id`) oraz stan CSRF (`ga4_oauth_state`), po czym przekierowuje
    użytkownika na ekran logowania/zgody Google."""
    audit = _get_owned_audit(request, pk)

    try:
        flow = Flow.from_client_secrets_file(
            str(settings.GA4_CLIENT_SECRETS_FILE),
            scopes=settings.GA4_SCOPES,
            redirect_uri=settings.GA4_REDIRECT_URI,
        )
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
    except FileNotFoundError:
        logger.error("Brak pliku client_secret.json (oczekiwana ścieżka: %s).", settings.GA4_CLIENT_SECRETS_FILE)
        messages.error(request, "Konfiguracja Google Analytics jest niekompletna - brak pliku client_secret.json.")
        return redirect("auditor:detail", pk=audit.pk)
    except Exception:
        logger.exception("Nie udało się zainicjować przepływu OAuth Google dla audytu %s.", audit.pk)
        messages.error(request, "Nie udało się rozpocząć logowania przez Google. Spróbuj ponownie.")
        return redirect("auditor:detail", pk=audit.pk)

    request.session["pending_audit_id"] = audit.pk
    request.session["ga4_oauth_state"] = state
    # google-auth-oauthlib generuje PKCE `code_verifier` przy tworzeniu URL-a autoryzacji
    # (flow.authorization_url), ale to inny obiekt `Flow` obsługuje callback (inny request/
    # proces) - bez zapisania go w sesji i odtworzenia w ga4_callback, flow.fetch_token()
    # kończy się błędem "InvalidGrantError: Missing code verifier".
    request.session["code_verifier"] = flow.code_verifier

    return redirect(authorization_url)


def _load_google_client_config() -> tuple[str, str]:
    """Odczytuje `client_id`/`client_secret` bezpośrednio z pliku `client_secret.json`,
    bez budowania pełnego obiektu `Flow` - potrzebne do odtworzenia `Credentials`
    z zapisanego wcześniej `refresh_token` (patrz `_build_credentials_from_refresh_token`)."""
    with open(settings.GA4_CLIENT_SECRETS_FILE, encoding="utf-8") as fh:
        raw_config = json.load(fh)
    config = raw_config.get("web") or raw_config.get("installed") or {}
    return config["client_id"], config["client_secret"]


def _build_credentials_from_refresh_token(audit: Audit):
    """Odtwarza `google.oauth2.credentials.Credentials` z `audit.ga4_refresh_token`,
    żeby móc odpytać GA4 bez ponownego przechodzenia przez ekran zgody Google."""
    client_id, client_secret = _load_google_client_config()
    return GA4OAuthService().build_credentials_from_refresh_token(
        refresh_token=audit.ga4_refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )


def _brand_token(url: str) -> str:
    """Wyciąga rdzeń nazwy domeny (bez schematu, "www." i TLD) do prostego
    dopasowania z nazwą wyświetlaną usługi GA4, np. "https://www.harbingers.io/"
    -> "harbingers", żeby móc podpowiedzieć właściwą usługę na liście wyboru."""
    normalized = url if "://" in url else f"https://{url}"
    domain = urlparse(normalized).netloc.lower()
    if domain.startswith("www."):
        domain = domain[len("www."):]
    return domain.split(".")[0] if domain else ""


def _ga4_properties_cache_key(audit_pk: int) -> str:
    """Klucz cache listy usług GA4 dla audytu.

    Lista siedzi w cache z własnym TTL, a nie w sesji: sesja rosła bez ograniczeń,
    bo porzucone przepływy OAuth zostawiały w niej wpisy na stałe.
    """
    return f"ga4_properties:{audit_pk}"


@login_required
def ga4_callback(request: HttpRequest) -> HttpResponse:
    """Odbiera kod autoryzacyjny z Google, wymienia go na `credentials` (w tym
    `refresh_token`), zapisuje token w powiązanym `Audit`, pobiera z Google Admin API
    listę wszystkich usług (properties) GA4 dostępnych dla zalogowanego konta i
    przekierowuje na stronę wyboru usługi (`select_ga4_property`) - konto Google może
    mieć dostęp do wielu usług GA4 i backend nie ma jak automatycznie ustalić, która
    z nich odpowiada audytowanej domenie."""
    audit_id = request.session.get("pending_audit_id")
    state = request.session.get("ga4_oauth_state")
    if not audit_id:
        messages.error(request, "Sesja autoryzacji Google wygasła. Spróbuj połączyć konto ponownie.")
        return redirect("auditor:index")

    audit = _get_owned_audit(request, audit_id)

    try:
        flow = Flow.from_client_secrets_file(
            str(settings.GA4_CLIENT_SECRETS_FILE),
            scopes=settings.GA4_SCOPES,
            state=state,
            redirect_uri=settings.GA4_REDIRECT_URI,
        )
        # Odtwarzamy PKCE code_verifier zapisany w start_ga4_auth - `flow` tworzony tutaj
        # to nowy obiekt (inny request niż ten, który wygenerował authorization_url), więc
        # bez tego flow.fetch_token() rzuca InvalidGrantError: "Missing code verifier".
        flow.code_verifier = request.session.get("code_verifier")
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        credentials = flow.credentials
    except Exception:
        logger.exception("Błąd podczas wymiany kodu autoryzacyjnego Google na token (audyt %s).", audit_id)
        messages.error(request, "Nie udało się połączyć z Google Analytics. Spróbuj ponownie.")
        return redirect("auditor:detail", pk=audit.pk)
    finally:
        request.session.pop("pending_audit_id", None)
        request.session.pop("ga4_oauth_state", None)
        request.session.pop("code_verifier", None)

    if credentials.refresh_token:
        # Setter właściwości szyfruje wartość, zapisujemy więc realne pole bazy.
        audit.ga4_refresh_token = credentials.refresh_token
        audit.save(update_fields=["ga4_refresh_token_encrypted"])
    else:
        logger.warning(
            "Google nie zwróciło refresh_token dla audytu %s - konto mogło już wcześniej wyrazić zgodę.", audit.pk
        )

    try:
        properties = GA4OAuthService().list_accessible_properties(credentials)
    except Exception:
        logger.exception("Nie udało się pobrać listy usług GA4 dla audytu %s.", audit.pk)
        properties = []

    if not properties:
        messages.warning(
            request,
            "Połączono z Google Analytics, ale to konto nie ma dostępu do żadnej usługi GA4.",
        )
        return redirect("auditor:detail", pk=audit.pk)

    cache.set(
        _ga4_properties_cache_key(audit.pk),
        properties,
        getattr(settings, "CACHE_TTL_GA4_PROPERTIES", 3600),
    )
    messages.success(request, "Połączono z Google. Wybierz teraz usługę Google Analytics 4.")
    return redirect("auditor:select_ga4_property", pk=audit.pk)


@login_required
def select_ga4_property(request: HttpRequest, pk: int) -> HttpResponse:
    """Krok pośredni po autoryzacji Google: prezentuje listę usług (properties) GA4
    dostępnych dla zalogowanego konta (pobraną w `ga4_callback`) i pozwala użytkownikowi
    ręcznie wskazać, która z nich odpowiada audytowanej domenie.

    Po zatwierdzeniu formularza (POST) zapisuje wybrany `ga4_property_id`, odtwarza
    `Credentials` z zapisanego `ga4_refresh_token` i przez `AuditService.sync_ga4_data`
    pobiera oraz zapisuje statystyki ruchu organicznego z GA4.
    """
    from .services.audit_service import AuditService

    audit = _get_owned_audit(request, pk)
    cache_key = _ga4_properties_cache_key(audit.pk)

    if request.method == "POST":
        property_id = request.POST.get("property_id", "").strip()
        if not property_id:
            messages.error(request, "Wybierz usługę Google Analytics 4 z listy.")
            return redirect("auditor:select_ga4_property", pk=audit.pk)

        if not audit.ga4_refresh_token:
            messages.error(request, "Brak zapisanego połączenia z Google - połącz konto ponownie.")
            return redirect("auditor:detail", pk=audit.pk)

        try:
            credentials = _build_credentials_from_refresh_token(audit)
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
            messages.error(request, "Konfiguracja Google Analytics jest niekompletna. Spróbuj połączyć konto ponownie.")
            return redirect("auditor:detail", pk=audit.pk)

        AuditService().sync_ga4_data(audit, credentials, property_id)
        cache.delete(cache_key)
        # Świeże dane GA4/GSC unieważniają zbuforowaną listę zdarzeń tej usługi.
        cache.delete(f"ga4_events:{property_id}")
        messages.success(request, "Wybrano usługę GA4 i pobrano dane o ruchu organicznym.")
        return redirect("auditor:detail", pk=audit.pk)

    properties = cache.get(cache_key) or []
    if not properties:
        messages.error(request, "Lista usług GA4 wygasła. Połącz konto Google ponownie.")
        return redirect("auditor:detail", pk=audit.pk)

    # Zaznaczamy co najwyżej JEDNĄ opcję (pierwsze dopasowanie) - <select> z wieloma
    # atrybutami "selected" jednocześnie jest niepoprawnym/mylącym znacznikiem HTML.
    brand_token = _brand_token(audit.url)
    has_auto_selected = False
    for prop in properties:
        is_match = not has_auto_selected and bool(brand_token) and brand_token in prop["display_name"].lower()
        prop["auto_selected"] = is_match
        has_auto_selected = has_auto_selected or is_match

    return render(
        request,
        "auditor/select_property.html",
        {"audit": audit, "properties": properties, "has_auto_selected": has_auto_selected},
    )


def _handle_ga4_lead_event_selection(request: HttpRequest, audit: Audit) -> None:
    """Zapisuje wybrane przez użytkownika zdarzenie lead/konwersja GA4 i odświeża
    pełną analitykę (dane wielokanałowe + automatyczne wnioski SEO) - wywoływane z
    formularza POST w `audit_detail`. Pusty wybór czyści `ga4_selected_lead_event`."""
    from .services.audit_service import AuditService

    event_name = request.POST.get("ga4_selected_lead_event", "").strip()

    if not audit.ga4_refresh_token or not audit.ga4_property_id:
        messages.error(request, "Połącz najpierw konto Google Analytics, żeby wybrać zdarzenie.")
        return

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        messages.error(request, "Sesja Google wygasła. Połącz konto ponownie.")
        return

    AuditService().refresh_ga4_lead_event(audit, credentials, event_name or None)
    messages.success(request, "Zaktualizowano analitykę GA4.")


def _fetch_ga4_available_events(audit: Audit) -> list[str]:
    """Lista zdarzeń GA4 do formularza wyboru leadu/konwersji.

    Wynik jest cache'owany: wcześniej każde wyświetlenie strony szczegółów oznaczało
    zapytanie do GA4 o 90 dni danych, co dokładało kilkaset milisekund do renderu i
    zużywało dzienną quotę przy zwykłym przeglądaniu raportu. Błąd (np. wygasły token)
    nie blokuje reszty strony - formularz po prostu nie pokaże wtedy żadnych opcji.
    """
    if not (audit.ga4_refresh_token and audit.ga4_property_id):
        return []

    cache_key = f"ga4_events:{audit.ga4_property_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    events: list[str] = []
    try:
        credentials = _build_credentials_from_refresh_token(audit)
        events = GA4OAuthService().get_available_events(credentials, audit.ga4_property_id)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s (lista zdarzeń GA4).", audit.pk)

    # Pustą listę też zapisujemy, ale na krócej - żeby błąd API nie powodował
    # odpytywania Google przy każdym odświeżeniu strony.
    ttl = getattr(settings, "CACHE_TTL_GA4_EVENTS", 6 * 3600) if events else 300
    cache.set(cache_key, events, ttl)
    return events


@login_required
def audit_detail(request: HttpRequest, pk: int) -> HttpResponse:
    audit = _get_owned_audit(request, pk)

    if request.method == "POST" and "ga4_selected_lead_event" in request.POST:
        _handle_ga4_lead_event_selection(request, audit)
        return redirect("auditor:detail", pk=audit.pk)

    all_metrics = annotate_metric_labels(list(audit.metrics.all()))

    # W podsumowaniu pomijamy osobne metryki score per urządzenie - reprezentuje je
    # jedna zbiorcza metryka "pagespeed_score".
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    critical_errors = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]
    warnings = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]
    passed_tests = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]
    info_tests = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.INFO]

    stats = {
        "errors_count": len(critical_errors),
        "warnings_count": len(warnings),
        "passed_count": len(passed_tests),
        "info_count": len(info_tests),
        # "Zgodne ze standardem" = testy zdane (OK) + opcjonalne (INFO) - oba nie są
        # problemem do naprawy, tylko WARNING/ERROR wymagają uwagi.
        "compliant_count": len(passed_tests) + len(info_tests),
        "total_count": len(summary_metrics),
    }

    # Zbiorcza flaga: czy na stronie w ogóle trzeba wczytać Chart.js (Senuto i/lub GA4
    # mają jakiekolwiek dane do narysowania). Liczona tutaj, a nie jako złożony warunek
    # and/or w szablonie, żeby uniknąć pomyłek z precedencją operatorów w templatce.
    ga4_lead_insights = audit.ga4_insights.get("lead_insights") or {}
    has_ga4 = bool(audit.ga4_refresh_token)
    show_charts_js = bool(
        audit.senuto_history.get("dates")
        or (has_ga4 and audit.ga4_history.get("dates"))
        or (has_ga4 and audit.ga4_channels_history.get("months"))
        or (has_ga4 and ga4_lead_insights.get("history", {}).get("months"))
    )

    return render(
        request,
        "auditor/detail.html",
        {
            "audit": audit,
            "critical_errors": critical_errors,
            "warnings": warnings,
            "passed_tests": passed_tests,
            "info_tests": info_tests,
            "stats": stats,
            # Zakładka "Audyt Techniczny": 4 tematyczne akordeony (Progressive Disclosure)
            # + dedykowana tabela statusów Schema.org, budowane z tych samych metryk.
            "technical_accordions": group_technical_accordions(summary_metrics),
            "schema_status_table": build_schema_status_table(summary_metrics),
            "category_scores": compute_category_scores(all_metrics) if audit.status == "completed" else [],
            "score_bucket": score_bucket(audit.score),
            "ga4_available_events": _fetch_ga4_available_events(audit),
            "ga4_lead_insights": ga4_lead_insights,
            "show_charts_js": show_charts_js,
            "audit_in_progress": audit.status in (Audit.Status.PENDING, Audit.Status.PROCESSING),
        },
    )


@login_required
def download_pdf_report(request: HttpRequest, audit_id: int) -> HttpResponse:
    """Generuje drukowalny (HTML -> Zapisz jako PDF w przeglądarce) raport z audytu,
    w formalnym układzie agencyjnym: okładka, spis treści, tabela priorytetów
    wdrożeniowych oraz rozdziały tematyczne."""
    audit = _get_owned_audit(request, audit_id)

    if audit.status != Audit.Status.COMPLETED:
        messages.error(request, "Raport PDF jest dostępny wyłącznie dla zakończonych audytów.")
        return redirect("auditor:detail", pk=audit.pk)

    all_metrics = annotate_metric_labels(list(audit.metrics.all()))
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    priority_findings = [
        m for m in summary_metrics
        if m.status in (AuditMetric.MetricStatus.ERROR, AuditMetric.MetricStatus.WARNING)
    ]
    for metric in priority_findings:
        metric.priority = priority_for_metric(metric)
        metric.team = TEAM_BY_CATEGORY.get(metric.category, "IT")
    priority_findings.sort(key=lambda m: m.priority, reverse=True)

    stats = {
        "errors_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]),
        "warnings_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]),
        "passed_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]),
    }

    return render(
        request,
        "auditor/report_pdf.html",
        {
            "audit": audit,
            "generated_at": timezone.now(),
            "category_scores": compute_category_scores(all_metrics),
            "score_bucket": score_bucket(audit.score),
            "stats": stats,
            "priority_findings": priority_findings,
            # Rozdział 1: Analiza Techniczna (technical + performance).
            "technical_metrics": [m for m in all_metrics if m.category in ("technical", "performance")],
            # Rozdział 2: Meta Tagi.
            "meta_tag_metrics": [m for m in all_metrics if m.category == "seo"],
            # Rozdział 3: Dane Strukturalne Schema.org.
            "schema_metrics": [m for m in all_metrics if m.key.startswith("schema_")],
            # Rozdział 4: Widoczność w AI Overviews (GEO) - pozostałe metryki structure.
            "geo_metrics": [
                m for m in all_metrics if m.category == "structure" and not m.key.startswith("schema_")
            ],
        },
    )
