"""Narzędzia agentów: cienkie opakowania na istniejące serwisy aplikacji.

Każde narzędzie zwraca dane, nie teksty do raportu - interpretacja należy do agenta.
Żadne nie podnosi wyjątku z zewnętrznego API: zamiast tego oddaje strukturę z polem
`error`, bo awaria jednego źródła nie może przerwać całego badania.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# Ile dni wstecz bierzemy jako "ostatni okres" przy trendach ruchu.
TRAFFIC_WINDOW_DAYS = 30

# Statusy metryk, które uznajemy za problem wymagający uwagi. `skipped` celowo nie -
# to metryka, której NIE DAŁO SIĘ zbadać (patrz auditor.services.accessibility),
# więc raportowanie jej jako błędu byłoby nieprawdą.
PROBLEM_STATUSES = ("error", "warning")


def find_audit_for_domain(domain_or_url: str, owner=None):
    """Najnowszy audyt techniczny tej domeny - albo None.

    Agenci pracują na domenie, nie na identyfikatorze audytu, więc powiązanie szukamy
    tutaj. Pomijamy rekordy założone wyłącznie dla analityki: nie mają metryk.
    """
    from auditor.models import Audit
    from auditor.services.google_api import audit_domain

    szukana = audit_domain(domain_or_url) or (domain_or_url or "").strip().lower()
    if not szukana:
        return None

    queryset = Audit.objects.filter(analytics_only=False)
    if owner is not None:
        queryset = queryset.filter(owner=owner)

    for audit in queryset.order_by("-created_at"):
        if audit_domain(audit.url) == szukana:
            return audit
    return None


def get_technical_health(domain_or_url: str, owner=None) -> dict:
    """Stan techniczny domeny: metryki ze ostatniego audytu albo świeży skan.

    Najpierw baza - audyt przeszedł pełny tor pobierania (rotacja User-Agenta,
    fallback przeglądarkowy dla stron CSR), więc jego dane są bogatsze niż pojedyncze
    żądanie. Bez audytu robimy lekki skan, żeby agent miał z czym pracować.

    Zwraca:
        {"source": "audit" | "scan", "audit_id": int | None, "score": int | None,
         "problems": [{"key", "category", "status", "value"}],
         "as_of": datetime | None, "error": str}

    `as_of` to data skanu, z którego pochodzą metryki. Przy świeżym skanie jest None -
    dane są z tej chwili, więc nie ma czego datować.
    """
    audit = find_audit_for_domain(domain_or_url, owner=owner)

    if audit is not None:
        problemy = [
            {
                "key": metric.key,
                "category": metric.category,
                "status": metric.status,
                "value": _metric_summary(metric),
            }
            for metric in audit.metrics.filter(status__in=PROBLEM_STATUSES)
        ]
        return {
            "source": "audit",
            "audit_id": audit.pk,
            "score": audit.score,
            "problems": problemy,
            # Metryki powstają w trakcie skanu i później się nie zmieniają, więc
            # data utworzenia audytu jest datą tych danych.
            "as_of": audit.created_at,
            "error": "",
        }

    return _scan_technical_health(domain_or_url)


def _metric_summary(metric) -> str:
    """Krótki opis metryki do wniosku agenta."""
    wartosc = metric.value if isinstance(metric.value, dict) else {}
    for klucz in ("note", "current_value", "value"):
        tekst = wartosc.get(klucz)
        if isinstance(tekst, str) and tekst.strip():
            return tekst.strip()[:200]
    return metric.key


def _scan_technical_health(domain_or_url: str) -> dict:
    """Lekki skan strony, gdy nie ma audytu w bazie."""
    from auditor.services.scraper import SEOScraper, ScraperError

    adres = domain_or_url if "://" in domain_or_url else f"https://{domain_or_url}"

    try:
        scraper = SEOScraper()
        dane = scraper.parse(scraper.fetch(adres), adres)
    except ScraperError as exc:
        return {
            "source": "scan", "audit_id": None, "score": None, "problems": [],
            "as_of": None, "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - awaria pobrania nie może wywrócić badania
        logger.exception("Skan techniczny %s nie powiódł się.", adres)
        return {
            "source": "scan",
            "audit_id": None,
            "score": None,
            "problems": [],
            "as_of": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    problemy = []
    if not dane.get("title"):
        problemy.append({"key": "title", "category": "seo", "status": "error",
                         "value": "Brak znacznika <title>."})
    if not dane.get("meta_description"):
        problemy.append({"key": "meta_description", "category": "seo", "status": "error",
                         "value": "Brak meta description."})
    if not dane.get("h1_non_empty_count"):
        problemy.append({"key": "h1_structure", "category": "technical", "status": "error",
                         "value": "Strona nie ma nagłówka H1."})
    elif dane.get("h1_non_empty_count", 0) > 1:
        problemy.append({"key": "h1_structure", "category": "technical", "status": "warning",
                         "value": f"Wiele nagłówków H1: {dane['h1_non_empty_count']}."})
    if dane.get("word_count", 0) < 300:
        problemy.append({"key": "content_length", "category": "technical", "status": "warning",
                         "value": f"Treść liczy {dane.get('word_count', 0)} słów."})

    return {"source": "scan", "audit_id": None, "score": None, "problems": problemy, "error": ""}


def get_traffic_trends(domain: str, owner=None) -> dict:
    """Trendy ruchu z GA4 i Search Console dla domeny.

    Pierwszeństwo ma BAZA DANYCH. Liczby zapisane przy podłączaniu usługi są pełne
    i nie wymagają ważnego tokena - token wygasa, dane nie. Odpytywanie Google po to,
    co już leży w bazie, uzależniałoby raport od stanu sesji OAuth zamiast od tego,
    co o domenie wiadomo. Do API sięgamy dopiero wtedy, gdy w bazie nie ma żadnych
    liczb.

    Zwraca:
        {"authorized": bool, "source": "db" | "api" | "",
         "ga4": {...} | None, "gsc": {...} | None, "error": str}

    `authorized` mówi WYŁĄCZNIE o żywym połączeniu z Google. Dane z bazy wracają
    z `authorized: False` i wypełnionym `source: "db"`, więc brak autoryzacji nie
    jest tożsamy z brakiem danych - i odwrotnie.
    """
    pusty = {"authorized": False, "source": "", "ga4": None, "gsc": None, "error": ""}

    rekordy = _audits_for_domain(domain, owner)
    if not rekordy:
        return {**pusty, "error": "Brak audytu ani rekordu analitycznego dla tej domeny."}

    zapisane = _stored_trends(rekordy)
    if zapisane is not None:
        return zapisane

    # Do API idziemy z rekordem, który ma wskazaną usługę GA4. Najnowszy rekord bywa
    # świeżym skanem technicznym bez przypisanej usługi, a usługa siedzi przy starszym -
    # wtedy pytanie o niego wracało z "nie przypisano usługi Analytics 4".
    audit = next((rekord for rekord in rekordy if rekord.ga4_property_id), rekordy[0])
    if not audit.has_google_credentials:
        return pusty

    from auditor.services.google_services import build_credentials_from_refresh_token

    try:
        credentials = build_credentials_from_refresh_token(audit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla %s.", domain)
        return {**pusty, "error": f"{type(exc).__name__}: {exc}"}

    if credentials is None:
        return {**pusty, "error": "Token Google jest nieczytelny - wymagane ponowne połączenie."}

    koniec = date.today()
    start = koniec - timedelta(days=TRAFFIC_WINDOW_DAYS)

    return {
        "authorized": True,
        "source": "api",
        "ga4": _ga4_trends(audit, credentials, start, koniec),
        "gsc": _gsc_trends(audit, credentials, start, koniec),
        "error": "",
    }


def _audits_for_domain(domain: str, owner) -> list:
    """Rekordy tej domeny od najnowszego - skany techniczne i wpisy analityczne razem.

    Jedna lista, bo dla analityki nie ma znaczenia, przy którym rodzaju rekordu
    zapisano liczby: użytkownik podłącza usługę do domeny, nie do audytu.
    """
    from auditor.models import Audit
    from auditor.services.google_api import audit_domain

    szukana = audit_domain(domain) or (domain or "").strip().lower()
    if not szukana:
        return []

    queryset = Audit.objects.all()
    if owner is not None:
        queryset = queryset.filter(owner=owner)

    return [
        audit
        for audit in queryset.order_by("-created_at")
        if audit_domain(audit.url) == szukana
    ]


def _stored_trends(audits: list) -> dict | None:
    """Trendy złożone z liczb już zapisanych w bazie - albo None, gdy ich nie ma.

    GA4 i Search Console przeglądamy osobno: usługi podłącza się niezależnie, więc
    liczby jednej mogą siedzieć w innym rekordzie niż liczby drugiej. Bierzemy
    pierwsze znalezione, czyli najnowsze.
    """
    ga4 = next((dane for dane in (_stored_ga4(audyt) for audyt in audits) if dane), None)
    gsc = next((dane for dane in (_stored_gsc(audyt) for audyt in audits) if dane), None)
    if ga4 is None and gsc is None:
        return None

    return {"authorized": False, "source": "db", "ga4": ga4, "gsc": gsc, "error": ""}


def _stored_ga4(audit) -> dict | None:
    """Liczby GA4 zapisane na jednym rekordzie - albo None."""
    if not audit.ga4_organic_sessions:
        return None

    wnioski = audit.ga4_insights or {}
    historia = audit.ga4_history or {}
    return {
        "property_id": audit.ga4_property_id or "",
        "sessions": audit.ga4_organic_sessions,
        # Świeżość liczymy z końca zapisanej serii, nie z daty rekordu: analitykę
        # da się odświeżyć bez zakładania nowego audytu, więc `created_at` pokazywałby
        # świeże dane jako stare.
        "as_of": _last_history_day(historia) or audit.created_at,
        # Okno czytamy z długości zapisanej historii: zakres dat bywa zmieniany
        # w panelu analityki, więc stałe 30 dni byłoby zgadywaniem.
        "window_days": len(historia.get("dates") or []) or None,
        "channels": {},
        # Zmiana policzona już przez `ga4_insights` (3 miesiące rok do roku) -
        # nie przeliczamy jej drugi raz, żeby nie rozjechała się z panelem.
        "organic_change_percent": wnioski.get("organic_change_pct"),
        "error": "",
    }


def _stored_gsc(audit) -> dict | None:
    """Liczby Search Console zapisane na jednym rekordzie - albo None."""
    if not audit.gsc_total_clicks_current:
        return None

    return {
        "site_url": audit.gsc_site_url or "(dopasowana po domenie)",
        "clicks_current": audit.gsc_total_clicks_current,
        "clicks_previous": audit.gsc_total_clicks_previous,
        "yoy_change_percent": audit.gsc_yoy_change_percent,
        "gainers": (audit.gsc_top_gainers or [])[:5],
        "losers": (audit.gsc_top_losers or [])[:5],
        # Zapisane frazy nie noszą własnych dat, więc zostaje data rekordu.
        "as_of": audit.created_at,
        "error": "",
    }


def _last_history_day(historia: dict):
    """Ostatni dzień zapisanej historii sesji - albo None, gdy jej nie ma."""
    dni = historia.get("dates") or []
    if not dni:
        return None

    from datetime import datetime

    try:
        return datetime.fromisoformat(max(dni)).date()
    except (TypeError, ValueError):
        return None


def _ga4_trends(audit, credentials, start: date, koniec: date) -> dict | None:
    """Sesje organiczne i porównanie rok do roku wg kanału."""
    if not audit.ga4_property_id:
        return None

    from auditor.services.ga4_service import GA4OAuthService

    service = GA4OAuthService()
    try:
        ruch = service.fetch_organic_traffic(credentials, audit.ga4_property_id, start, koniec)
        yoy = service.fetch_yoy_summary(credentials, audit.ga4_property_id, start, koniec)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Nie udało się pobrać danych GA4 dla %s.", audit.url)
        return {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "property_id": audit.ga4_property_id,
        "sessions": ruch.get("total_sessions", 0),
        "window_days": (koniec - start).days,
        "channels": yoy.get("channels", {}),
        "error": "",
    }


def _gsc_trends(audit, credentials, start: date, koniec: date) -> dict | None:
    """Kliknięcia i frazy rok do roku z Search Console."""
    from auditor.services.gsc_service import GSCService

    service = GSCService()
    try:
        frazy = service.fetch_yoy_query_performance(credentials, audit.url, start, koniec)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Nie udało się pobrać danych Search Console dla %s.", audit.url)
        return {"error": f"{type(exc).__name__}: {exc}"}

    # `GSCService` zwraca płaskie klucze (`total_clicks_current`, `top_gainers`),
    # a nie zagnieżdżone `totals`/`gainers` - przekładamy je na kształt oczekiwany
    # przez agenta, żeby ten nie musiał znać szczegółów serwisu.
    return {
        "site_url": audit.gsc_site_url or "(dopasowana po domenie)",
        "clicks_current": frazy.get("total_clicks_current", 0),
        "clicks_previous": frazy.get("total_clicks_previous", 0),
        "yoy_change_percent": frazy.get("yoy_change_percent"),
        "gainers": (frazy.get("top_gainers") or [])[:5],
        "losers": (frazy.get("top_losers") or [])[:5],
        "error": "",
    }


def get_geo_visibility(domain: str, owner=None) -> dict:
    """Widoczność marki w wyszukiwarkach AI - z ostatniego badania GEO.

    Nie uruchamiamy nowego badania: kosztuje kilkadziesiąt wywołań modelu i trwa
    minuty. Agent korzysta z tego, co już zmierzono.
    """
    from auditor.models import GeoStudy
    from auditor.services.geo import normalize_domain

    szukana = normalize_domain(domain)
    queryset = GeoStudy.objects.filter(status=GeoStudy.Status.COMPLETED)
    if owner is not None:
        queryset = queryset.filter(owner=owner)

    study = (
        queryset.filter(domain=szukana).prefetch_related("queries__runs").order_by("-created_at").first()
    )
    if study is None:
        return {"measured": False, "measured_at": None, "error": ""}

    from auditor.presentation import build_geo_executive_summary

    queries = list(study.queries.all())
    podsumowanie = build_geo_executive_summary(study, queries)

    return {
        "measured": True,
        "overall_score": podsumowanie["overall_score"],
        "totals": podsumowanie["totals"],
        "top_competitors": podsumowanie["top_competitors_list"],
        "measured_at": study.created_at,
        "error": "",
    }
