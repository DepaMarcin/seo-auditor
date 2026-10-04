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
import secrets
from datetime import date
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_GET, require_POST
from google_auth_oauthlib.flow import Flow

from .models import Audit, AuditedPage, AuditMetric
from .navigation import TOOLS
from .presentation import (
    MERGED_PAGESPEED_SCORE_KEYS,
    TEAM_BY_CATEGORY,
    annotate_metric_labels,
    build_geo_benchmark,
    build_geo_executive_summary,
    build_geo_questions,
    build_geo_repetition_stats,
    build_geo_sources,
    build_geo_visibility_totals,
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
from .services.google_services import (
    apply_google_services,
    build_credentials_from_refresh_token,
    load_google_client_config,
)
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


def _visible_audits(request: HttpRequest):
    """Audyty widoczne dla tego użytkownika.

    Superużytkownik widzi wszystkie - to konto administracyjne, które i tak ma dostęp
    do każdego rekordu przez panel /admin/. Zwykły użytkownik wyłącznie swoje.
    """
    queryset = Audit.objects.all()
    if not request.user.is_superuser:
        queryset = queryset.filter(owner=request.user)
    return queryset


def _visible_scans(request: HttpRequest):
    """Audyty techniczne - bez rekordów założonych wyłącznie dla analityki.

    Rekord `analytics_only` nie ma metryk ani wyniku skanowania, więc na liście
    audytów byłby pustym wierszem z zerowym wynikiem.
    """
    return _visible_audits(request).filter(analytics_only=False)


def _visible_geo_studies(request: HttpRequest):
    """Badania GEO widoczne dla tego użytkownika - ta sama zasada co przy audytach."""
    from auditor.models import GeoStudy

    queryset = GeoStudy.objects.all()
    if not request.user.is_superuser:
        queryset = queryset.filter(owner=request.user)
    return queryset


def _get_owned_audit(request: HttpRequest, pk: int) -> Audit:
    """Pobiera audyt widoczny dla zalogowanego użytkownika albo zwraca 404.

    Świadomie 404, a nie 403: brak audytu i brak uprawnień muszą wyglądać identycznie,
    żeby nie dało się przez kod odpowiedzi ustalić, które identyfikatory istnieją.
    """
    return get_object_or_404(_visible_audits(request), pk=pk)


@method_decorator(login_required, name="dispatch")
class HubView(View):
    """Ekran wyboru narzędzia - pierwsze, co użytkownik widzi po zalogowaniu.

    Kafelki są danymi, a nie trzema kopiami tego samego HTML-a: dzięki temu dodanie
    czwartego narzędzia to jeden wpis, a nie kolejne powielenie znaczników.
    """

    template_name = "auditor/hub.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, {"tools": TOOLS})


@login_required
def analytics_dashboard(request: HttpRequest) -> HttpResponse:
    """`/analytics/` - od razu dashboard albo ekran podłączenia.

    Bez listy domen w środku: strona pokazuje analitykę bieżącej domeny, a przełącznik
    w nagłówku pozwala zmienić ją w jednym kroku. Pośrednia lista była dodatkowym
    kliknięciem przed każdym spojrzeniem na dane.

    Bieżąca domena to ta z `?audit=`, a bez niej - najnowszy audyt użytkownika.
    """
    audits = list(_visible_audits(request).order_by("-created_at")[:RECENT_AUDITS_LIMIT])
    google = _google_access_context(request)

    # Wskazanie audytu wprost (z raportu, z przepływu OAuth) omija ekran wejściowy.
    wskazany = _audit_from_parameter(request, audits)
    if wskazany is not None:
        return _render_analytics(request, wskazany, audits)

    szukana = (request.GET.get("domain") or "").strip()
    if not szukana:
        # Czysty ekran wejściowy: konto Google i jedno pole. Lista starych skanów
        # technicznych nie ma tu czego szukać - mówi o audytach, nie o dostępach.
        return render(request, "auditor/analytics_entry.html", {
            "nav_section": "analytics",
            "google": google,
            "connect_audit": audits[0] if audits else None,
        })

    return _open_domain_analytics(request, szukana, google, audits)


def _open_domain_analytics(request: HttpRequest, wanted: str, google: dict, audits: list):
    """Otwiera analitykę wpisanej domeny - po sprawdzeniu uprawnień w Google."""
    from auditor.services.google_api import audit_domain, find_authorized_domain

    if not google["connected"]:
        messages.error(
            request, "Najpierw podłącz konto Google, żeby zobaczyć dane analityczne."
        )
        return redirect("auditor:analytics")

    uprawnienie = find_authorized_domain(google["domains"], wanted)
    if uprawnienie is None:
        # Kluczowa odmowa: bez dostępu w Google nie przypisujemy niczego i nie
        # udajemy, że domena jest podłączona.
        messages.error(
            request,
            f"Brak dostępu do usługi GA4/GSC dla domeny {wanted} na zalogowanym "
            f"koncie Google [{google['email'] or 'nieznane'}]. "
            "Przełącz konto albo poproś o uprawnienia w Google.",
        )
        return redirect("auditor:analytics")

    docelowy = next(
        (a for a in audits if audit_domain(a.url) == uprawnienie["domain"]
         or audit_domain(a.url).endswith("." + uprawnienie["domain"])
         or uprawnienie["domain"].endswith("." + audit_domain(a.url))),
        None,
    )

    # Brak audytu technicznego nie jest przeszkodą: analityka opisuje domenę, nie
    # wynik skanowania. Zakładamy lekki rekord, bo wybór usługi i pobrane liczby
    # muszą mieć gdzie leżeć - ale nie udajemy, że powstał audyt.
    if docelowy is None:
        docelowy = _create_analytics_record(request, uprawnienie)

    return redirect("auditor:audit_analytics", pk=docelowy.pk)


def _create_analytics_record(request: HttpRequest, uprawnienie: dict) -> Audit:
    """Zakłada rekord analityczny dla domeny z konta Google.

    `analytics_only` odróżnia go od audytu technicznego: nie ma metryk, nie pojawia
    się na liście skanera i nikt go nie zlecał. Istnieje wyłącznie jako miejsce na
    przypisanie usługi GA4 i pobrane dane.
    """
    from auditor.services.google_services import apply_google_services

    audit = Audit.objects.create(
        url=f"https://{uprawnienie['domain']}/",
        owner=request.user,
        analytics_only=True,
        status=Audit.Status.COMPLETED,
    )

    # Gdy konto ma dokładnie jedną usługę GA4 dla tej domeny, nie ma o co pytać -
    # przypisujemy ją i pobieramy dane, żeby dashboard od razu coś pokazał.
    uslugi = uprawnienie.get("ga4") or []
    if len(uslugi) == 1:
        try:
            apply_google_services(audit, str(uslugi[0].get("property_id", "")))
        except Exception:  # noqa: BLE001 - brak danych nie może zablokować wejścia
            logger.exception("Nie udało się pobrać danych dla nowego rekordu %s.", audit.pk)

    logger.info(
        "Utworzono rekord analityczny dla %s (audyt techniczny nie jest wymagany).",
        uprawnienie["domain"],
    )
    return audit


@login_required
def audit_analytics(request: HttpRequest, pk: int) -> HttpResponse:
    """Analityka wskazanego audytu - ten sam ekran, inny punkt wejścia.

    Zostaje, bo prowadzą tu odnośniki z raportu i z przepływu OAuth.
    """
    audit = _get_owned_audit(request, pk)
    audits = list(_visible_audits(request).order_by("-created_at")[:RECENT_AUDITS_LIMIT])

    # Audyt starszy niż limit listy nie znalazłby się w przełączniku, a ma być
    # tam widoczny jako bieżący.
    if all(pozycja.pk != audit.pk for pozycja in audits):
        audits.insert(0, audit)

    return _render_analytics(request, audit, audits)


def _audit_from_parameter(request: HttpRequest, audits: list):
    """Audyt wskazany w `?audit=`, albo None, gdy nie wskazano żadnego.

    Numer spoza własnych audytów traktujemy jak brak wskazania - cudzej analityki
    nie pokazujemy, a 404 na wejściu z hubu byłoby mylące.
    """
    wskazany = request.GET.get("audit", "")
    if not wskazany.isdigit():
        return None

    for audit in audits:
        if audit.pk == int(wskazany):
            return audit
    return None


def _render_analytics(request: HttpRequest, audit: "Audit", audits: list) -> HttpResponse:
    """Wspólne renderowanie obu stanów analityki."""
    google = _google_account_context(audit)

    from .services.google_api import audit_domain, gsc_site_domain

    # Rozbieżność domen zwykle znaczy, że usługę przypisano omyłkowo nie temu
    # audytowi - pokazujemy to wprost, zamiast prezentować cudze liczby jako swoje.
    niespojna_witryna = bool(
        audit.gsc_site_url
        and gsc_site_domain(audit.gsc_site_url) != audit_domain(audit.url)
    )

    return render(request, "auditor/analytics_dashboard.html", {
        "nav_section": "analytics",
        "audit": audit,
        "audits": audits,
        "google": google,
        "selectors": _audit_with_selectors(audit, google),
        # Dwa stany, nie więcej: brak przypisanej usługi albo gotowy dashboard.
        "has_analytics": bool(audit.ga4_property_id),
        "mismatched_site": niespojna_witryna,
        "gsc_site_domain": gsc_site_domain(audit.gsc_site_url) if audit.gsc_site_url else "",
        "audit_domain": audit_domain(audit.url),
        "today_iso": timezone.localdate().isoformat(),
    })


def _user_google_account(request: HttpRequest):
    """Konto Google zalogowanego użytkownika - albo None.

    Jedno poświadczenie na użytkownika. Wcześniej token leżał przy każdym audycie
    osobno, więc aplikacja brała "jakiś" z kilkunastu i uznawała powiązaną domenę
    za podłączoną także wtedy, gdy konto Google nie miało do niej dostępu.
    """
    from auditor.models import GoogleAccount

    return GoogleAccount.objects.filter(user=request.user).first()


def _google_access_context(request: HttpRequest) -> dict:
    """Stan konta Google i lista domen, do których MA ono realny dostęp.

    Uprawnienia czytamy z Google (GA4 Account Summaries + GSC sites list), nie z
    naszej bazy: wpis w bazie mówi tylko, że ktoś kiedyś coś przypisał.
    """
    from auditor.services.google_api import fetch_account_email, list_authorized_domains
    from auditor.services.google_services import build_credentials_from_refresh_token

    pusty = {
        "connected": False,
        "email": "",
        "domains": [],
        "error": "",
    }

    konto = _user_google_account(request)
    if konto is None or not konto.is_connected:
        return pusty

    class _Nosnik:
        """Minimalny obiekt dla `build_credentials_from_refresh_token`."""

        def __init__(self, token, pk):
            self.ga4_refresh_token = token
            self.pk = pk

    try:
        credentials = build_credentials_from_refresh_token(
            _Nosnik(konto.refresh_token, konto.pk)
        )
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się odtworzyć poświadczeń konta Google %s.", konto.pk)
        return {**pusty, "error": "token"}

    if credentials is None:
        return {**pusty, "error": "token"}

    email = konto.email or fetch_account_email(credentials)
    if email and email != konto.email:
        konto.email = email
        konto.save(update_fields=["email"])

    return {
        "connected": True,
        "email": email,
        "domains": list_authorized_domains(credentials),
        "error": "",
    }


def _google_account_context(audit) -> dict:
    """Stan połączenia z Google dla audytu - używane przez dashboard pojedynczej domeny.

    Wszystko pochodzi z jednego tokenu, więc pobieramy raz na żądanie i dzielimy
    między wiersze - inaczej lista dziesięciu audytów oznaczałaby dziesięć
    kompletów zapytań do Google.
    """
    from auditor.services.crypto import ENCRYPTED_PREFIX
    from auditor.services.google_api import fetch_account_email, list_gsc_sites

    pusty = {
        "connected": False,
        "email": "",
        "token_encrypted": False,
        "ga4_properties": [],
        "gsc_sites": [],
        "connect_audit": None,
        "error": "",
    }
    # Bez zapisanego tokenu nie ma czego odtwarzać. Sprawdzamy to WPROST, bo
    # `build_credentials_from_refresh_token` zwraca obiekt poświadczeń także dla
    # pustego tokenu - kod uznawał wtedy konto za połączone i odpytywał Google
    # przy każdym otwarciu strony, czekając na timeout.
    if audit is None or not audit.has_google_credentials:
        return pusty if audit is None else {**pusty, "connect_audit": audit}

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except Exception:  # noqa: BLE001 - brak konfiguracji OAuth nie może wywrócić panelu
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        credentials = None

    if credentials is None:
        # Token jest w bazie, ale nie da się go odczytać (zmieniony klucz szyfrujący)
        # albo stracił ważność - dla użytkownika to tyle samo co brak połączenia.
        return {**pusty, "connect_audit": audit, "error": "token"}

    email = audit.ga4_account_email or fetch_account_email(credentials)
    if email and email != audit.ga4_account_email:
        audit.ga4_account_email = email
        audit.save(update_fields=["ga4_account_email"])

    try:
        properties = GA4OAuthService().list_accessible_properties(credentials)
    except Exception:  # noqa: BLE001 - panel ma się wyrenderować mimo awarii Google
        logger.exception("Nie udało się pobrać listy usług GA4.")
        properties = []

    return {
        "connected": True,
        "email": email,
        "token_encrypted": (audit.ga4_refresh_token_encrypted or "").startswith(
            ENCRYPTED_PREFIX
        ),
        "ga4_properties": properties,
        "gsc_sites": list_gsc_sites(credentials),
        "connect_audit": audit,
        "error": "",
    }


def _audit_with_selectors(audit, google: dict) -> dict:
    """Audyt wraz z listami wyboru usługi GA4 i witryny GSC.

    Opcje pasujące do domeny audytu trafiają na początek listy z adnotacją
    "(Sugerowana)" - przy kilkunastu usługach na koncie to różnica między wyborem
    a szukaniem.
    """
    from auditor.services.google_api import audit_domain, mark_suggestions

    domena = audit_domain(audit.url)

    ga4_options = mark_suggestions(
        [
            {
                "value": str(wlasciwosc.get("property_id", "")),
                "label": wlasciwosc.get("display_name") or wlasciwosc.get("property_id", ""),
                "account": wlasciwosc.get("account_name", ""),
                "match": (wlasciwosc.get("display_name") or "").lower(),
            }
            for wlasciwosc in google.get("ga4_properties", [])
        ],
        domena,
        "match",
        mode="text",
    )

    gsc_options = mark_suggestions(
        [
            {
                "value": witryna["site_url"],
                "label": witryna["label"],
                "match": witryna["domain"],
            }
            for witryna in google.get("gsc_sites", [])
        ],
        domena,
        "match",
    )

    return {
        "audit": audit,
        "domain": domena,
        # Token jest zapisany przy audycie - dopóki go nie ma, wiersz pokazuje
        # własny przycisk rozpoczynający autoryzację właśnie dla niego.
        "audit_has_token": bool(audit.ga4_refresh_token_encrypted),
        "ga4_options": ga4_options,
        "gsc_options": gsc_options,
    }


def _reset_assignments_after_account_change(request: HttpRequest, nowy_email: str) -> None:
    """Czyści przypisania usług, gdy podłączono inne konto Google.

    Usługa GA4 i witryna Search Console zapisane z poprzedniego konta mogą być
    niedostępne dla nowego - a raport pokazywałby wtedy liczby, których bieżące
    konto nie ma prawa widzieć.
    """
    from .services.google_services import GA4_DERIVED_FIELDS, GSC_DERIVED_FIELDS, reset_derived_fields

    do_czyszczenia = _visible_audits(request).exclude(
        ga4_property_id__isnull=True, gsc_site_url=""
    )

    liczba = 0
    for audit in do_czyszczenia:
        if audit.ga4_account_email and audit.ga4_account_email == nowy_email:
            # To samo konto co wcześniej - przypisania zostają w mocy.
            continue

        audit.ga4_property_id = None
        audit.gsc_site_url = ""
        audit.ga4_account_email = nowy_email
        pola = ["ga4_property_id", "gsc_site_url", "ga4_account_email"]
        pola += reset_derived_fields(audit, GA4_DERIVED_FIELDS)
        pola += reset_derived_fields(audit, GSC_DERIVED_FIELDS)
        audit.save(update_fields=pola)
        liczba += 1

    if liczba:
        logger.info("Zmiana konta Google: wyczyszczono przypisania w %s audytach.", liczba)


@login_required
@require_POST
def google_disconnect(request: HttpRequest) -> HttpResponse:
    """Odłącza konto Google użytkownika i unieważnia przypisania usług.

    Poświadczenie jest jedno, przy koncie użytkownika. Czyścimy też przypisane usługi:
    bez tokenu nie da się ich odpytać, a zostawione sugerowałyby działające połączenie.
    """
    from .services.google_services import GA4_DERIVED_FIELDS, GSC_DERIVED_FIELDS, reset_derived_fields

    konto = _user_google_account(request)
    if konto is not None:
        konto.delete()

    liczba = 0
    for audit in _visible_audits(request):
        if not (audit.ga4_property_id or audit.gsc_site_url or audit.ga4_refresh_token_encrypted):
            continue
        audit.ga4_property_id = None
        audit.gsc_site_url = ""
        audit.ga4_account_email = ""
        # Kopia tokenu sprzed wdrożenia `GoogleAccount` też musi zniknąć.
        audit.ga4_refresh_token_encrypted = None
        pola = [
            "ga4_property_id",
            "gsc_site_url",
            "ga4_account_email",
            "ga4_refresh_token_encrypted",
        ]
        pola += reset_derived_fields(audit, GA4_DERIVED_FIELDS)
        pola += reset_derived_fields(audit, GSC_DERIVED_FIELDS)
        audit.save(update_fields=pola)
        liczba += 1

    messages.success(
        request,
        f"Odłączono konto Google (wyczyszczono przypisania w {liczba} audytach).",
    )

    # Wracamy tam, skąd przyszło żądanie - odłączenie wywołuje się z dashboardu audytu.
    wrocic_do = request.POST.get("audit")
    if wrocic_do and wrocic_do.isdigit():
        return redirect("auditor:audit_analytics", pk=int(wrocic_do))
    return redirect("auditor:analytics")


# Dokąd wrócić po zapisie. Przyjmujemy wyłącznie te dwie nazwy, a nie dowolny adres
# z formularza - parametr `next` z żądania jest wartością od klienta i posłużyłby do
# przekierowania użytkownika poza aplikację.
ASSIGNMENT_RETURN_TARGETS = {
    # Po zapisie wracamy na dashboard analityki TEGO audytu - tam widać skutek.
    "analytics": "auditor:audit_analytics",
    "detail": "auditor:detail",
}
# Audyt, którego analitykę właśnie zapisano, staje się bieżącą domeną na /analytics/.
DEFAULT_RETURN_TARGET = "analytics"


@login_required
@require_POST
def assign_google_services(request: HttpRequest, pk: int) -> HttpResponse:
    """Jedyne miejsce przypisujące usługi Google do audytu.

    Obsługuje obie sytuacje: pierwszy wybór zaraz po autoryzacji OAuth i późniejszą
    zmianę z panelu analityki. Wcześniej były to dwie osobne ścieżki, które robiły
    prawie to samo - "prawie", bo tylko jedna czyściła dane poprzedniej usługi, więc
    wynik zależał od tego, którędy użytkownik przyszedł.

    Parametr `next` decyduje wyłącznie o tym, dokąd wrócić: po autoryzacji naturalnym
    celem jest raport audytu, przy zmianie z panelu - lista audytów.
    """
    audit = _get_owned_audit(request, pk)

    property_id = (request.POST.get("ga4_property_id") or "").strip()
    powrot = request.POST.get("next") or DEFAULT_RETURN_TARGET

    # Przypisanie usługi, do której konto Google nie ma dostępu, dałoby raport
    # pełen cudzych albo pustych liczb pod nazwą tej domeny. Sprawdzamy uprawnienia
    # w Google ZANIM cokolwiek zapiszemy.
    if property_id and not _has_access_to_property(request, property_id):
        google = _google_access_context(request)
        messages.error(
            request,
            f"Brak dostępu do usługi GA4 o identyfikatorze {property_id} na zalogowanym "
            f"koncie Google [{google['email'] or 'nieznane'}]. Nic nie zostało zapisane.",
        )
        return _redirect_after_assignment(audit, powrot)

    # Witryny Search Console nie czytamy z formularza: dobiera ją automat po domenie
    # audytu. Ręczny wybór produkował przypisania w poprzek projektów, których
    # użytkownik nie miał jak zauważyć.
    wynik = apply_google_services(audit, property_id)

    # Brak zmian nie wymaga komunikatu: użytkownik widzi te same ustawienia, a
    # powtarzane zapisy zostawiały za sobą kolejkę identycznych powiadomień.
    if not wynik.changed:
        pass
    elif wynik.fetched:
        messages.success(
            request, f"Zapisano ustawienia i pobrano świeże dane dla {audit.url}."
        )
    else:
        messages.warning(
            request,
            f"Zapisano ustawienia dla {audit.url}, ale nie udało się pobrać danych "
            "z Google. Dane poprzedniej usługi zostały wyczyszczone - odśwież raport "
            "za chwilę.",
        )

    return _redirect_after_assignment(audit, powrot)


def _has_access_to_property(request: HttpRequest, property_id: str) -> bool:
    """Czy zalogowane konto Google ma dostęp do tej usługi GA4.

    Pytamy Google, a nie bazę: wpis w bazie mówi tylko, że ktoś kiedyś coś przypisał.
    Gdy Google nie odpowiada, nie blokujemy zapisu - inaczej awaria po ich stronie
    uniemożliwiałaby pracę, a sam zapis nie jest operacją niebezpieczną.
    """
    from auditor.services.google_api import list_ga4_properties
    from auditor.services.google_services import build_credentials_from_refresh_token

    konto = _user_google_account(request)
    if konto is None or not konto.is_connected:
        return False

    class _Nosnik:
        def __init__(self, token, pk):
            self.ga4_refresh_token = token
            self.pk = pk

    try:
        credentials = build_credentials_from_refresh_token(
            _Nosnik(konto.refresh_token, konto.pk)
        )
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się odtworzyć poświadczeń konta Google %s.", konto.pk)
        return True

    wlasciwosci = list_ga4_properties(credentials)
    if not wlasciwosci:
        # Pusta lista znaczy albo brak dostępu, albo awarię pobrania - nie da się
        # tego rozróżnić, więc nie blokujemy.
        return True

    return any(str(w.get("property_id")) == property_id for w in wlasciwosci)


def _redirect_after_assignment(audit: Audit, target: str) -> HttpResponse:
    """Przekierowanie po zapisie - zawsze w kontekście TEGO audytu."""
    nazwa = ASSIGNMENT_RETURN_TARGETS.get(target, ASSIGNMENT_RETURN_TARGETS[DEFAULT_RETURN_TARGET])
    return redirect(nazwa, pk=audit.pk)


def _refresh_google_data(audit: Audit, property_id: str, site_url: str) -> bool:
    """Pobiera dane GA4 i GSC dla świeżo przypisanych usług.

    Zwraca True, gdy cokolwiek udało się pobrać. Brak tokenu albo awaria Google nie
    jest tu błędem krytycznym - użytkownik zobaczy ostrzeżenie, a nie stronę błędu.
    """
    from .services.audit_service import AuditService

    if not audit.ga4_refresh_token:
        return False

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        return False

    service = AuditService()
    pobrano = False

    if property_id:
        try:
            service.sync_ga4_data(audit, credentials, property_id)
            pobrano = True
        except Exception:  # noqa: BLE001
            logger.exception("Nie udało się pobrać danych GA4 dla audytu %s.", audit.pk)

    try:
        service.sync_gsc_data(audit, credentials)
        pobrano = True
    except Exception:  # noqa: BLE001
        logger.exception("Nie udało się pobrać danych Search Console dla audytu %s.", audit.pk)

    return pobrano


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

    audits = _visible_scans(request).order_by("-created_at")[:RECENT_AUDITS_LIMIT]
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
        # "select_account" wymusza ekran wyboru konta - bez niego Google loguje
        # po cichu na konto już aktywne w przeglądarce i przełączenie jest niemożliwe.
        prompt = "consent select_account" if request.GET.get("switch") else "consent"
        # Do losowego stanu doklejamy numer audytu - dzięki temu callback wie, dla
        # którego audytu przyszła odpowiedź, nawet gdy w międzyczasie użytkownik
        # zaczął łączyć inny audyt w drugiej karcie przeglądarki.
        losowy_stan = secrets.token_urlsafe(32)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt=prompt,
            include_granted_scopes="true",
            state=_build_oauth_state(
                audit.pk, losowy_stan, request.GET.get("return_to", OAUTH_RETURN_SELECT)
            ),
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


# Odtwarzanie poświadczeń mieszka w warstwie serwisowej razem z resztą obsługi
# Google. Te dwie nazwy zostają jako aliasy, bo używa ich kilkanaście widoków.
#
# Uwaga dla testów: alias kopiuje referencję w czasie importu, więc podmiana
# `auditor.services.google_services.build_credentials_from_refresh_token` NIE wpłynie
# na widoki. Patchuj `auditor.views._build_credentials_from_refresh_token`.
_load_google_client_config = load_google_client_config
_build_credentials_from_refresh_token = build_credentials_from_refresh_token


def _brand_token(url: str) -> str:
    """Wyciąga rdzeń nazwy domeny (bez schematu, "www." i TLD) do prostego
    dopasowania z nazwą wyświetlaną usługi GA4, np. "https://www.harbingers.io/"
    -> "harbingers", żeby móc podpowiedzieć właściwą usługę na liście wyboru."""
    normalized = url if "://" in url else f"https://{url}"
    domain = urlparse(normalized).netloc.lower()
    if domain.startswith("www."):
        domain = domain[len("www."):]
    return domain.split(".")[0] if domain else ""


# Parametr `state` wraca z Google nienaruszony, więc to w nim wieziemy numer audytu.
# Sesja tego nie załatwia: dwie karty przeglądarki dzielą jedną sesję, a każde
# rozpoczęcie autoryzacji nadpisuje `pending_audit_id` - wraca wtedy audyt z karty,
# którą kliknięto później, a nie ten, dla którego przyszła odpowiedź.
OAUTH_STATE_SEPARATOR = ":"

# Dokąd wrócić po autoryzacji. "select" to ekran wyboru usługi dla tego audytu -
# naturalne zakończenie podłączania konkretnego audytu. "analytics" to panel; tam
# wracamy po przełączeniu konta Google, bo token jest wspólny dla wszystkich audytów
# i wepchnięcie użytkownika w konfigurację jednego z nich byłoby przypadkowe.
OAUTH_RETURN_SELECT = "select"
OAUTH_RETURN_ANALYTICS = "analytics"
OAUTH_RETURN_TARGETS = (OAUTH_RETURN_SELECT, OAUTH_RETURN_ANALYTICS)


def _build_oauth_state(audit_pk: int, csrf_state: str, return_to: str = OAUTH_RETURN_SELECT) -> str:
    """Skleja numer audytu, cel powrotu i losowy stan CSRF w jeden parametr `state`."""
    if return_to not in OAUTH_RETURN_TARGETS:
        return_to = OAUTH_RETURN_SELECT
    return OAUTH_STATE_SEPARATOR.join((str(audit_pk), return_to, csrf_state))


def _audit_pk_from_state(state: str) -> int | None:
    """Wyciąga numer audytu z `state`. Zwraca None, gdy parametr jest nie nasz."""
    if not state or OAUTH_STATE_SEPARATOR not in state:
        return None
    prefix = state.split(OAUTH_STATE_SEPARATOR, 1)[0]
    return int(prefix) if prefix.isdigit() else None


def _return_target_from_state(state: str) -> str:
    """Cel powrotu zapisany w `state`; domyślnie ekran wyboru usługi."""
    czesci = (state or "").split(OAUTH_STATE_SEPARATOR)
    if len(czesci) >= 2 and czesci[1] in OAUTH_RETURN_TARGETS:
        return czesci[1]
    return OAUTH_RETURN_SELECT


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
    # Numer audytu bierzemy z `state` zwróconego przez Google, a nie z sesji: to
    # jedyna wartość związana z TĄ konkretną odpowiedzią. Sesja zostaje jako zapas
    # dla przepływów rozpoczętych przed tą zmianą.
    state = request.GET.get("state") or request.session.get("ga4_oauth_state")
    audit_id = _audit_pk_from_state(state) or request.session.get("pending_audit_id")

    if not audit_id:
        messages.error(request, "Sesja autoryzacji Google wygasła. Spróbuj połączyć konto ponownie.")
        return redirect("auditor:index")

    # `_get_owned_audit` zwróci 404, gdy numer w `state` wskazuje cudzy audyt -
    # parametr wraca od klienta, więc nie jest zaufany.
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
        # Token zapisujemy przy KONCIE użytkownika, nie przy audycie. Kopia per audyt
        # rozsiewała po bazie kilkanaście niezależnych poświadczeń i prowadziła do
        # fałszywych statusów "podłączone" dla domen bez dostępu w Google.
        from auditor.models import GoogleAccount
        from auditor.services.google_api import fetch_account_email

        konto, _ = GoogleAccount.objects.get_or_create(user=request.user)
        konto.refresh_token = credentials.refresh_token
        konto.email = fetch_account_email(credentials) or konto.email
        konto.save(update_fields=["refresh_token_encrypted", "email", "connected_at"])

        # Zmiana konta unieważnia wszystko, co przypisano z poprzedniego: usługi
        # i liczby mogły pochodzić z zasobów, do których nowe konto nie ma dostępu.
        _reset_assignments_after_account_change(request, konto.email)
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
    # Jeden komunikat na jedno zdarzenie. Wcześniej przy przełączaniu konta padały
    # dwa pod rząd, bo każda gałąź dokładała swój do wspólnego.
    if _return_target_from_state(state) == OAUTH_RETURN_ANALYTICS:
        messages.success(request, "Połączono konto Google. Wskaż usługę dla audytu.")
        return redirect("auditor:audit_analytics", pk=audit.pk)

    messages.success(request, "Połączono z Google. Wybierz teraz usługę Google Analytics 4.")
    return redirect("auditor:select_ga4_property", pk=audit.pk)


@login_required
@require_GET
def select_ga4_property(request: HttpRequest, pk: int) -> HttpResponse:
    """Ekran wyboru usługi GA4 pokazywany zaraz po autoryzacji Google.

    Sam już nie zapisuje - formularz kieruje do `assign_google_services`, czyli tam,
    gdzie trafia też zmiana z panelu analityki. Wcześniej zapis był tu zdublowany
    i różnił się zachowaniem: ta ścieżka nie czyściła danych poprzedniej usługi, więc
    wynik zależał od tego, którędy użytkownik przyszedł.
    """
    audit = _get_owned_audit(request, pk)
    cache_key = _ga4_properties_cache_key(audit.pk)

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


# ======================================================================
# Widoczność w wyszukiwarkach AI (GEO Tracker)
# ======================================================================
# Jeden komunikat dla obu ścieżek (przycisk "Wygeneruj przez AI" i start badania),
# żeby użytkownik nie dostawał dwóch różnych opisów tej samej sytuacji.
SITE_CONTEXT_ERROR = (
    "Nie udało się pobrać treści podanego adresu URL. Upewnij się, że adres jest "
    "poprawny lub wpisz pytania testowe ręcznie."
)


def _owned_geo_study(request: HttpRequest, pk: int):
    """Badanie należące do zalogowanego użytkownika albo 404.

    Ta sama zasada co przy audytach: badanie zawiera dane konkurencyjne klienta,
    więc znajomość identyfikatora nie może wystarczać do jego odczytania.
    """
    return get_object_or_404(_visible_geo_studies(request), pk=pk)


@method_decorator(login_required, name="dispatch")
class GeoVisibilityDashboardView(View):
    """Lista badań GEO i formularz uruchomienia nowego."""

    template_name = "auditor/geo_dashboard.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        from auditor.models import GeoStudy

        studies = (
            _visible_geo_studies(request)
            .select_related("audit")
            .prefetch_related("queries")[:20]
        )
        return render(request, self.template_name, {
            "nav_section": "geo",
            "studies": studies,
            "audits": _visible_scans(request)[:20],
            "default_questions": [],
        })

    def post(self, request: HttpRequest) -> HttpResponse:
        from auditor.models import GeoQuery, GeoStudy
        from auditor.services.geo import DEFAULT_REPETITIONS, normalize_domain
        from auditor.tasks import enqueue_geo_study

        # Zachowujemy to, co wpisał użytkownik: `normalize_domain` obcina ścieżkę,
        # a kontekst branżowy pobieramy z konkretnej podstrony, jeśli ją podał.
        wpisany_adres = (request.POST.get("domain", "") or "").strip()
        domain = normalize_domain(wpisany_adres)
        if not domain:
            messages.error(request, "Podaj domenę albo adres strony do zbadania.")
            return redirect("auditor:geo_dashboard")

        # Adres walidujemy tą samą ochroną co każdy inny pobierany przez serwer -
        # badanie GEO nie odpytuje domeny bezpośrednio, ale trafia ona do promptu
        # i do raportu, więc nie może być adresem z sieci lokalnej ani schematem
        # innym niż http/https.
        try:
            validate_public_url(domain)
        except UnsafeUrlError as exc:
            messages.error(request, str(exc))
            return redirect("auditor:geo_dashboard")

        questions = [q.strip() for q in request.POST.getlist("questions") if q.strip()]
        context = None
        if not questions:
            # Użytkownik nie podał pytań - układamy je sami, wcześniej rozpoznając
            # branżę z treści strony. Bez tego kroku model zgaduje po nazwie domeny.
            from auditor.services.geo import generate_questions, get_or_fetch_site_context

            context = get_or_fetch_site_context(wpisany_adres or domain)
            if not context.usable:
                messages.warning(
                    request,
                    SITE_CONTEXT_ERROR + " Badanie ruszy na pytaniach ogólnych.",
                )
            questions = generate_questions(domain, context=context)

        audit = None
        audit_id = request.POST.get("audit")
        if audit_id:
            audit = Audit.objects.filter(pk=audit_id, owner=request.user).first()

        # Marka jest potrzebna do wykrywania wzmianek w treści odpowiedzi, więc
        # ustalamy ją także wtedy, gdy użytkownik wpisał pytania sam i kontekstu
        # strony w ogóle nie pobieraliśmy - wtedy wystarczy nazwa z domeny.
        from auditor.services.geo import extract_brand_name, parse_competitors_input

        brand_name = context.brand_name if context and context.brand_name else extract_brand_name(
            wpisany_adres or domain
        )
        competitors = parse_competitors_input(
            request.POST.get("competitors", ""), exclude=domain
        )

        study = GeoStudy.objects.create(
            owner=request.user,
            audit=audit,
            domain=domain,
            brand_name=brand_name,
            competitors_input=competitors,
            repetitions=DEFAULT_REPETITIONS,
        )
        GeoQuery.objects.bulk_create([
            GeoQuery(study=study, text=text, position=index)
            for index, text in enumerate(questions, start=1)
        ])

        enqueue_geo_study(study.pk)
        return redirect("auditor:geo_detail", pk=study.pk)


@login_required
def geo_study_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Dashboard pojedynczego badania: wynik zbiorczy, powtarzalność, pytania, źródła."""
    study = _owned_geo_study(request, pk)
    queries = list(study.queries.prefetch_related("runs").all())

    return render(request, "auditor/geo_detail.html", {
        "nav_section": "geo",
        "study": study,
        "queries": queries,
        # Odpowiedzi modelu przechodzą przez parser Markdown: bez tego użytkownik
        # widzi "**[CHEERS]**" i adresy URL na pół ekranu.
        "questions": build_geo_questions(study, queries),
        "repetition_stats": build_geo_repetition_stats(study, queries),
        "sources": build_geo_sources(study, queries),
        "benchmark": build_geo_benchmark(study, queries),
        "summary": build_geo_executive_summary(study, queries),
        # Jedna liczba dla karty KPI, tabeli porównawczej i podsumowania.
        "visibility": build_geo_visibility_totals(queries),
        "in_progress": study.status in ("pending", "processing"),
    })


@login_required
@require_POST
def geo_study_rerun(request: HttpRequest, pk: int) -> HttpResponse:
    """Powtarza badanie na tych samych pytaniach.

    Nowe badanie zamiast nadpisania starego: sens pomiaru GEO polega na porównywaniu
    kolejnych pomiarów w czasie, więc poprzedni wynik musi zostać.
    """
    from auditor.models import GeoQuery, GeoStudy
    from auditor.tasks import enqueue_geo_study

    study = _owned_geo_study(request, pk)

    if is_rate_limited(request, scope="geo"):
        messages.error(request, "Przekroczono limit uruchamianych badań. Spróbuj później.")
        return redirect("auditor:geo_detail", pk=study.pk)

    powtorzone = GeoStudy.objects.create(
        owner=request.user,
        audit=study.audit,
        domain=study.domain,
        brand_name=study.brand_name,
        competitors_input=study.competitors_input,
        repetitions=study.repetitions,
    )
    GeoQuery.objects.bulk_create([
        GeoQuery(study=powtorzone, text=query.text, position=query.position)
        for query in study.queries.all()
    ])

    enqueue_geo_study(powtorzone.pk)
    messages.success(request, "Badanie uruchomione ponownie na tych samych pytaniach.")
    return redirect("auditor:geo_detail", pk=powtorzone.pk)


@login_required
def geo_study_status(request: HttpRequest, pk: int) -> JsonResponse:
    """Postęp badania - odpytywane przez pasek postępu w przeglądarce."""
    study = _owned_geo_study(request, pk)
    done = study.completed_runs
    total = study.total_runs
    query_count = study.queries.count()
    # "Badanie zapytania 3 z 5" - numer pytania wynika z liczby wykonanych powtórzeń.
    current_query = min(done // study.repetitions + 1, query_count) if study.repetitions else 0

    return JsonResponse({
        "status": study.status,
        "done": done,
        "total": total,
        "percent": study.progress_percent,
        "current_query": current_query if study.status == "processing" else query_count,
        "query_count": query_count,
        "overall_score": study.overall_score,
        "error": study.error,
    })


@login_required
def geo_suggest_questions(request: HttpRequest) -> JsonResponse:
    """Podpowiada pytania intencyjne dla domeny (przycisk "Wygeneruj przez AI").

    Pytania powstają z treści wskazanej strony, a nie z samej nazwy domeny - dopiero
    oferta przed oczami modelu daje pytania z właściwej branży.
    """
    from auditor.services.geo import (
        generate_questions,
        get_or_fetch_site_context,
        normalize_domain,
    )

    wpisany_adres = (request.POST.get("domain", "") or request.GET.get("domain", "")).strip()
    domain = normalize_domain(wpisany_adres)
    if not domain:
        return JsonResponse({"error": "Podaj domenę."}, status=400)

    try:
        validate_public_url(domain)
    except UnsafeUrlError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    context = get_or_fetch_site_context(wpisany_adres or domain)
    if not context.usable:
        return JsonResponse({"error": SITE_CONTEXT_ERROR}, status=502)

    return JsonResponse({
        "questions": generate_questions(domain, context=context),
        "context_source": context.source,
        "detected_title": context.title,
    })

