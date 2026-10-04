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
         "problems": [{"key", "category", "status", "value"}], "error": str}
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
        return {"source": "scan", "audit_id": None, "score": None, "problems": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - awaria pobrania nie może wywrócić badania
        logger.exception("Skan techniczny %s nie powiódł się.", adres)
        return {
            "source": "scan",
            "audit_id": None,
            "score": None,
            "problems": [],
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

    Wymaga podłączonego konta Google właściciela i przypisanej usługi GA4. Brak
    autoryzacji NIE jest błędem narzędzia - zwracamy `authorized: False`, a badanie
    idzie dalej bez tej części.

    Zwraca:
        {"authorized": bool, "ga4": {...} | None, "gsc": {...} | None, "error": str}
    """
    pusty = {"authorized": False, "ga4": None, "gsc": None, "error": ""}

    audit = find_audit_for_domain(domain, owner=owner) or _analytics_record(domain, owner)
    if audit is None:
        return {**pusty, "error": "Brak audytu ani rekordu analitycznego dla tej domeny."}

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
        "ga4": _ga4_trends(audit, credentials, start, koniec),
        "gsc": _gsc_trends(audit, credentials, start, koniec),
        "error": "",
    }


def _analytics_record(domain: str, owner):
    """Rekord analityczny tej domeny (założony bez audytu technicznego)."""
    from auditor.models import Audit
    from auditor.services.google_api import audit_domain

    szukana = audit_domain(domain) or (domain or "").strip().lower()
    queryset = Audit.objects.filter(analytics_only=True)
    if owner is not None:
        queryset = queryset.filter(owner=owner)

    for audit in queryset.order_by("-created_at"):
        if audit_domain(audit.url) == szukana:
            return audit
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
        return {"measured": False, "error": ""}

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
