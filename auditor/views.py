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
from datetime import date
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from google_auth_oauthlib.flow import Flow

from .models import Audit, AuditedPage, AuditMetric
from .presentation import (
    MERGED_PAGESPEED_SCORE_KEYS,
    TEAM_BY_CATEGORY,
    annotate_metric_labels,
    build_schema_status_table,
    compute_category_scores,
    extract_pagespeed_summary,
    extract_schema_cards,
    group_technical_accordions,
    pagespeed_score_bucket,
    priority_for_metric,
    score_bucket,
)
from .ratelimit import is_rate_limited
from .services.date_ranges import (
    DateRangeError,
    default_range,
    format_range_label,
    parse_iso_date,
    quick_range,
    validate_range,
)
from .services.ga4_insights import analyze_channel_trends
from .services.ga4_service import GA4OAuthService
from .services.gsc_insights import generate_page_commentary, generate_query_commentary
from .services.gsc_service import GSCService
from .services.exporter import build_report, report_filename
from .services.sheets import GoogleSheetsService, MissingSheetsScopeError, SheetsExportError
from .services.sitemap import SitemapService
from .services.spreadsheet import build_csv, build_xlsx
from .services.url_guard import UnsafeUrlError, validate_public_url
from .tasks import enqueue_audit

logger = logging.getLogger(__name__)

# Liczba audytów na liście na stronie głównej.
RECENT_AUDITS_LIMIT = 10

# Zakres pokazywany w sekcjach GA4/GSC przy pierwszym wejściu na stronę audytu.
DEFAULT_ANALYTICS_RANGE_DAYS = 30

# Dodatkowe szablony podstron w formularzu nowego audytu. Kolejność odpowiada kolejności
# pól na ekranie, a `page_type` musi pochodzić z `AuditedPage.PageType`.
TEMPLATE_SLOTS = [
    {"field": "url_category", "page_type": AuditedPage.PageType.CATEGORY,
     "label": "Strona kategorii", "placeholder": "przyklad.pl/kategoria/buty"},
    {"field": "url_product", "page_type": AuditedPage.PageType.PRODUCT,
     "label": "Strona produktu", "placeholder": "przyklad.pl/produkt/but-sportowy"},
    {"field": "url_blog", "page_type": AuditedPage.PageType.BLOG,
     "label": "Wpis na blogu", "placeholder": "przyklad.pl/blog/jak-dobrac-buty"},
    {"field": "url_offer", "page_type": AuditedPage.PageType.OFFER,
     "label": "Strona ofertowa", "placeholder": "przyklad.pl/oferta"},
]


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

        try:
            template_pages = _collect_template_pages(request, primary_url=url)
        except UnsafeUrlError as exc:
            messages.error(request, f"Adres dodatkowego szablonu jest nieprawidłowy: {exc}")
            return redirect("auditor:index")

        audit = Audit.objects.create(url=url, owner=request.user)
        if template_pages:
            AuditedPage.objects.bulk_create([
                AuditedPage(audit=audit, url=page_url, page_type=page_type)
                for page_url, page_type in template_pages
            ])
        # Audyt trwa minuty (PageSpeed + rekomendacje AI), więc leci w tle - strona
        # szczegółów odpytuje potem `audit_status` i odświeża się po zakończeniu.
        enqueue_audit(audit.pk)
        return redirect("auditor:detail", pk=audit.pk)

    audits = Audit.objects.filter(owner=request.user).order_by("-created_at")[:RECENT_AUDITS_LIMIT]
    return render(
        request,
        "auditor/index.html",
        {"audits": audits, "template_slots": TEMPLATE_SLOTS},
    )


def _collect_template_pages(request: HttpRequest, primary_url: str) -> list[tuple[str, str]]:
    """Odczytuje z formularza adresy dodatkowych szablonów podstron.

    Każdy adres przechodzi tę samą walidację co adres główny (`validate_public_url`) -
    podstrony są skanowane przez serwer, więc dotyczy ich dokładnie to samo ryzyko SSRF.
    Duplikaty (także powtórzenie adresu głównego) są pomijane, bo `AuditedPage` ma
    ograniczenie unikalności na parę (audyt, adres).
    """
    pages: list[tuple[str, str]] = []
    seen = {primary_url.rstrip("/")}

    for slot in TEMPLATE_SLOTS:
        raw_url = request.POST.get(slot["field"], "").strip()
        if not raw_url:
            continue

        safe_url = validate_public_url(raw_url)
        if safe_url.rstrip("/") in seen:
            continue

        seen.add(safe_url.rstrip("/"))
        pages.append((safe_url, slot["page_type"]))

    return pages


@login_required
def sitemap_suggestions(request: HttpRequest) -> JsonResponse:
    """Podpowiedzi adresów per szablon, wyciągnięte z `sitemap.xml` audytowanej domeny.

    Odpytywany asynchronicznie z formularza nowego audytu. Brak mapy witryny nie jest
    błędem - użytkownik po prostu uzupełnia adresy ręcznie.
    """
    url = request.GET.get("url", "").strip()
    if not url:
        return JsonResponse({"error": "Podaj adres domeny."}, status=400)

    # Ten sam licznik co przy uruchamianiu audytu - parsowanie mapy witryny to kilka
    # żądań HTTP do obcego serwera, więc nie może być wywoływane bez ograniczeń.
    if is_rate_limited(request, scope="sitemap"):
        return JsonResponse({"error": "Zbyt wiele zapytań o mapę witryny. Spróbuj za chwilę."}, status=429)

    result = SitemapService().suggest_pages(url)
    return JsonResponse({
        "available": result["available"],
        "sitemap_url": result["sitemap_url"],
        "suggestions": result["suggestions"],
        "scanned_urls": result["scanned_urls"],
        "error": result["error"],
    })


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


def _resolve_requested_range(request: HttpRequest) -> tuple[date, date]:
    """Wyznacza zakres dat żądania: `range` (skrót) albo `start_date`/`end_date`.

    Podnosi `DateRangeError` z komunikatem gotowym do pokazania użytkownikowi, gdy
    parametry są niepoprawne (zły format, odwrócona kolejność, zbyt szerokie okno).
    """
    quick = request.GET.get("range", "").strip()
    if quick:
        return quick_range(quick)

    raw_start = request.GET.get("start_date", "").strip()
    raw_end = request.GET.get("end_date", "").strip()
    if not raw_start and not raw_end:
        return default_range(DEFAULT_ANALYTICS_RANGE_DAYS)
    if not raw_start or not raw_end:
        raise DateRangeError("Podaj obie daty zakresu (start_date oraz end_date).")

    start = parse_iso_date(raw_start, "start_date")
    end = parse_iso_date(raw_end, "end_date")
    return validate_range(start, end)


def _build_ga4_payload(
    audit: Audit, start_date: date, end_date: date, period_label: str
) -> dict:
    """Świeże dane GA4 dla wskazanego zakresu: szereg czasowy, KPI i przeliczone wnioski."""
    if not (audit.ga4_refresh_token and audit.ga4_property_id):
        return {"available": False, "reason": "Nie połączono konta Google Analytics."}

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        return {"available": False, "reason": "Sesja Google wygasła - połącz konto ponownie."}

    service = GA4OAuthService()
    property_id = audit.ga4_property_id
    lead_event = audit.ga4_selected_lead_event

    traffic = service.fetch_organic_traffic(credentials, property_id, start_date, end_date)
    channels = service.fetch_channel_history(credentials, property_id, start_date, end_date)

    lead_history = None
    if lead_event:
        lead_history = service.fetch_event_conversions(
            credentials, property_id, lead_event, start_date, end_date
        )["history"]

    yoy = service.fetch_yoy_summary(
        credentials, property_id, start_date, end_date, lead_event_name=lead_event
    )
    insights = analyze_channel_trends(
        yoy["channels"], lead_history=lead_history, lead_totals_3m=yoy["leads"],
        period_label=period_label,
    )

    return {
        "available": True,
        "organic_sessions": traffic["total_sessions"],
        "granularity": traffic["granularity"],
        "history": traffic["history"],
        "channels": channels,
        "insights": insights,
        "lead_event": lead_event,
        "lead_insights": insights.get("lead_insights") or {},
    }


def _build_gsc_payload(
    audit: Audit, start_date: date, end_date: date, period_label: str
) -> dict:
    """Świeże dane Search Console dla wskazanego zakresu (vs ten sam okres rok wcześniej)."""
    if not (audit.ga4_refresh_token and audit.ga4_property_id):
        return {"available": False, "reason": "Nie połączono konta Google."}

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        return {"available": False, "reason": "Sesja Google wygasła - połącz konto ponownie."}

    service = GSCService()
    query_stats = service.fetch_yoy_query_performance(credentials, audit.url, start_date, end_date)
    page_stats = service.fetch_yoy_page_performance(credentials, audit.url, start_date, end_date)

    return {
        "available": True,
        "total_clicks_current": query_stats["total_clicks_current"],
        "total_clicks_previous": query_stats["total_clicks_previous"],
        "yoy_change_percent": query_stats["yoy_change_percent"],
        "top_gainers": query_stats["top_gainers"],
        "top_losers": query_stats["top_losers"],
        "top_page_gainers": page_stats["top_gainers"],
        "top_page_losers": page_stats["top_losers"],
        "query_commentary": generate_query_commentary(query_stats, period_label),
        "page_commentary": generate_page_commentary(page_stats, period_label),
    }


@login_required
def analytics_data(request: HttpRequest, pk: int) -> JsonResponse:
    """Endpoint JSON zasilający dynamiczny wybór zakresu dat w sekcjach GA4 i GSC.

    Parametry (query string):
      * `start_date`, `end_date` - zakres w formacie YYYY-MM-DD, albo
      * `range` - skrót: "7d" / "30d" / "90d" / "12m",
      * `source` - "ga4", "gsc" albo "all" (domyślnie): która sekcja ma być policzona.
        Ogranicza liczbę wywołań API do tej sekcji, którą użytkownik faktycznie zmienił.

    Zwraca 400 z czytelnym komunikatem, gdy zakres jest niepoprawny - walidacja leży
    w `auditor.services.date_ranges.validate_range`.
    """
    audit = _get_owned_audit(request, pk)

    try:
        start_date, end_date = _resolve_requested_range(request)
    except DateRangeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    source = request.GET.get("source", "all").strip().lower()
    if source not in ("all", "ga4", "gsc"):
        return JsonResponse({"error": "Nieprawidłowa wartość parametru source."}, status=400)

    period_label = format_range_label(start_date, end_date)
    payload: dict = {
        "range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "label": period_label,
        }
    }

    if source in ("all", "ga4"):
        payload["ga4"] = _build_ga4_payload(audit, start_date, end_date, period_label)
    if source in ("all", "gsc"):
        payload["gsc"] = _build_gsc_payload(audit, start_date, end_date, period_label)

    return JsonResponse(payload)


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

    # Sekcja "Krytyczne problemy i ostrzeżenia" na szczycie zakładki technicznej:
    # wszystkie testy ze statusem ERROR/WARNING, błędy przed ostrzeżeniami.
    priority_findings = critical_errors + warnings

    pagespeed_summary = extract_pagespeed_summary(summary_metrics)
    # Pełna karta testu (z rekomendacją AI) ma pojawić się na stronie DOKŁADNIE raz:
    # przy błędzie/ostrzeżeniu pokazuje ją panel priorytetów, w pozostałych przypadkach
    # - panel PageSpeed. Sam panel PageSpeed zawsze rysuje wskaźniki punktowe.
    pagespeed_card_in_panel = bool(pagespeed_summary) and pagespeed_summary not in priority_findings

    ga4_lead_insights = audit.ga4_insights.get("lead_insights") or {}
    has_ga4 = bool(audit.ga4_refresh_token)
    # Zbiorcza flaga: czy na stronie w ogóle trzeba wczytać Chart.js (Senuto i/lub GA4
    # mają jakiekolwiek dane do narysowania). Liczona tutaj, a nie jako złożony warunek
    # and/or w szablonie, żeby uniknąć pomyłek z precedencją operatorów w templatce.
    # Przy połączonym GA4 Chart.js jest potrzebny zawsze - wykresy powstają nawet z
    # pustymi danymi, żeby dynamiczna zmiana zakresu dat miała co aktualizować.
    show_charts_js = bool(has_ga4 or audit.senuto_history.get("dates"))

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
            "priority_findings": priority_findings,
            "technical_accordions": group_technical_accordions(summary_metrics),
            "schema_status_table": build_schema_status_table(summary_metrics),
            "schema_cards": extract_schema_cards(summary_metrics),
            # Dedykowany panel podsumowania PageSpeed na szczycie zakładki technicznej -
            # metryka jest wyłączona z akordeonów, żeby nie dublować tej samej karty.
            "pagespeed_summary": pagespeed_summary,
            "pagespeed_card_in_panel": pagespeed_card_in_panel,
            # Zestawienie przebadanych szablonów podstron (auditor.models.AuditedPage).
            "audited_pages": list(audit.pages.all()),
            "pagespeed_mobile_bucket": pagespeed_score_bucket(
                pagespeed_summary.value.get("mobile_score") if pagespeed_summary else None
            ),
            "pagespeed_desktop_bucket": pagespeed_score_bucket(
                pagespeed_summary.value.get("desktop_score") if pagespeed_summary else None
            ),
            "category_scores": compute_category_scores(all_metrics) if audit.status == "completed" else [],
            "score_bucket": score_bucket(audit.score),
            "ga4_available_events": _fetch_ga4_available_events(audit),
            "ga4_lead_insights": ga4_lead_insights,
            "show_charts_js": show_charts_js,
            # Górna granica pól <input type="date"> - nie ma danych z przyszłości.
            "today_iso": timezone.localdate().isoformat(),
            "audit_in_progress": audit.status in (Audit.Status.PENDING, Audit.Status.PROCESSING),
        },
    )


# ----------------------------------------------------------------------
# Eksport raportu: plik do pobrania (XLSX/CSV) albo arkusz Google Sheets
# ----------------------------------------------------------------------

EXPORT_CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}


@login_required
def export_report(request: HttpRequest, pk: int) -> HttpResponse:
    """Serwuje raport audytu jako plik do pobrania (`?format=xlsx` albo `?format=csv`)."""
    audit = _get_owned_audit(request, pk)

    export_format = request.GET.get("format", "xlsx").strip().lower()
    if export_format not in EXPORT_CONTENT_TYPES:
        messages.error(request, "Nieobsługiwany format eksportu - wybierz XLSX albo CSV.")
        return redirect("auditor:detail", pk=audit.pk)

    sheets = build_report(audit)

    if export_format == "xlsx":
        payload: bytes = build_xlsx(sheets)
    else:
        # BOM pozwala Excelowi rozpoznać UTF-8 - bez niego polskie znaki w CSV
        # wyświetlają się jako "krzaki" przy otwarciu podwójnym kliknięciem.
        payload = build_csv(sheets).encode("utf-8-sig")

    response = HttpResponse(payload, content_type=EXPORT_CONTENT_TYPES[export_format])
    response["Content-Disposition"] = f'attachment; filename="{report_filename(audit, export_format)}"'
    return response


@login_required
def export_to_google_sheets(request: HttpRequest, pk: int) -> HttpResponse:
    """Tworzy arkusz z raportem na koncie Google użytkownika i przekierowuje do niego.

    Wymaga POST - utworzenie arkusza jest operacją zapisującą na koncie użytkownika,
    więc nie może dać się wywołać zwykłym odnośnikiem (ochrona CSRF).
    """
    audit = _get_owned_audit(request, pk)

    if request.method != "POST":
        return redirect("auditor:detail", pk=audit.pk)

    if not audit.ga4_refresh_token:
        messages.error(
            request,
            "Połącz konto Google (sekcja GA4), żeby wyeksportować raport do Google Sheets.",
        )
        return redirect("auditor:detail", pk=audit.pk)

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        messages.error(request, "Konfiguracja Google jest niekompletna. Połącz konto ponownie.")
        return redirect("auditor:detail", pk=audit.pk)

    title = f"Audyt SEO - {urlparse(audit.url).netloc or audit.url} ({audit.created_at:%Y-%m-%d})"

    try:
        spreadsheet_url = GoogleSheetsService().create_report(credentials, title, build_report(audit))
    except MissingSheetsScopeError as exc:
        # Tokeny wydane przed dodaniem zakresu `spreadsheets` nie mają uprawnienia do
        # arkuszy - użytkownik musi jednorazowo przejść ekran zgody Google ponownie.
        messages.error(request, str(exc))
        return redirect("auditor:detail", pk=audit.pk)
    except SheetsExportError as exc:
        messages.error(request, str(exc))
        return redirect("auditor:detail", pk=audit.pk)

    messages.success(request, "Raport został utworzony w Google Sheets.")
    return redirect(spreadsheet_url)


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
