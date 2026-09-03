from __future__ import annotations

import logging
from datetime import date
from urllib.parse import urlparse

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .date_ranges import last_n_full_months_range, same_months_last_year

logger = logging.getLogger(__name__)

QUERY_ROW_LIMIT = 25000
PERIOD_MONTHS = 3
TOP_N = 10


def _extract_base_domain(audit_url: str) -> str:
    """Wyciąga czystą, bazową nazwę domeny z dowolnego adresu audytu - bez schematu,
    "www.", ścieżki i portu, np. "https://www.motivationdirect.pl/en/" ->
    "motivationdirect.pl"."""
    normalized = audit_url if "://" in audit_url else f"https://{audit_url}"
    domain = urlparse(normalized).netloc.lower()
    domain = domain.split(":")[0]  # odetnij ewentualny port
    return domain[len("www."):] if domain.startswith("www.") else domain


def find_best_gsc_site(service, audit_url: str) -> str | None:
    """Znajduje usługę (property) Search Console najlepiej odpowiadającą `audit_url`
    wśród wszystkich usług dostępnych dla zalogowanego konta Google.

    GSC pozwala zarejestrować tę samą domenę na kilka różnych sposobów - jako usługę
    domenową ("sc-domain:example.com"), jako URL-prefix z protokołem http/https, z
    "www." lub bez - dotychczasowe ścisłe porównanie ciągów znaków nie znajdowało
    właściwej usługi, gdy się różniły formatem. Dopasowanie odbywa się wg priorytetu
    (od najbardziej precyzyjnego):
        1. Usługa domenowa "sc-domain:<bazowa domena>".
        2. Dokładny `audit_url` (po normalizacji końcowego "/").
        3. Warianty URL z/bez "www." (http oraz https).
        4. Dowolna usługa, której URL zawiera bazową domenę.

    Zwraca `siteUrl` najlepiej pasującej usługi, albo None, gdy żadna nie pasuje lub
    nie udało się pobrać listy usług.
    """
    domain = _extract_base_domain(audit_url)
    if not domain:
        print(f"[GSC] Nie udało się wyodrębnić domeny z adresu audytu: {audit_url!r}.")
        return None

    try:
        response = service.sites().list().execute()
    except Exception:
        logger.exception("Nie udało się pobrać listy usług Search Console.")
        print("[GSC] Błąd podczas pobierania listy usług Search Console (sites().list()).")
        return None

    site_urls = [entry.get("siteUrl", "") for entry in response.get("siteEntry", []) if entry.get("siteUrl")]
    if not site_urls:
        print(f"[GSC] Konto Google nie ma dostępu do żadnej usługi Search Console (domena audytu: {domain}).")
        return None

    # 1. Usługa domenowa - najbardziej precyzyjne i najczęściej spotykane dopasowanie.
    domain_property = f"sc-domain:{domain}"
    for site_url in site_urls:
        if site_url.lower() == domain_property:
            print(f"[GSC] Dopasowano usługę domenową '{site_url}' dla domeny {domain}.")
            return site_url

    # 2. Dokładny URL audytu (po normalizacji końcowego ukośnika).
    normalized_audit_url = audit_url.rstrip("/").lower()
    for site_url in site_urls:
        if site_url.rstrip("/").lower() == normalized_audit_url:
            print(f"[GSC] Dopasowano dokładny URL usługi '{site_url}' dla {audit_url}.")
            return site_url

    # 3. Warianty URL z/bez "www.", http oraz https.
    url_variants = {
        f"https://{domain}/", f"http://{domain}/",
        f"https://www.{domain}/", f"http://www.{domain}/",
    }
    for site_url in site_urls:
        if site_url.lower() in url_variants:
            print(f"[GSC] Dopasowano wariant URL usługi '{site_url}' dla domeny {domain}.")
            return site_url

    # 4. Dowolna usługa, której URL zawiera bazową domenę (np. inna subdomena/ścieżka).
    for site_url in site_urls:
        normalized = site_url.lower().replace("sc-domain:", "")
        if domain in normalized:
            print(f"[GSC] Dopasowano usługę '{site_url}' po zawieraniu domeny {domain}.")
            return site_url

    print(f"[GSC] Nie znaleziono usługi Search Console dla domeny {domain}. Dostępne usługi konta: {site_urls}")
    return None


class GSCService:
    """Klient Google Search Console API (Search Analytics), autoryzowany przez OAuth
    2.0 (ten sam przepływ "Zaloguj się przez Google" co GA4, ze scope'em
    `webmasters.readonly` - patrz `settings.GA4_SCOPES`). Porównuje wydajność fraz
    kluczowych oraz podstron: ostatnie 3 pełne miesiące vs analogiczne 3 miesiące
    rok temu (3M R/R).

    Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie
    (SenutoService, GA4OAuthService): błąd komunikacji z GSC (brak dostępu, domena
    niezarejestrowana w Search Console, wygasły token) nigdy nie podnosi wyjątku na
    zewnątrz - zwracany jest bezpieczny fallback z zerowymi wartościami.
    """

    def resolve_site_url(self, credentials: Credentials, audit_url: str) -> str | None:
        """Wygodny skrót: buduje klienta Search Console i zwraca `find_best_gsc_site`
        dla `audit_url` - do sprawdzenia, czy/która usługa GSC pasuje do domeny, bez
        pobierania właściwych danych o kliknięciach."""
        try:
            service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
        except Exception:
            logger.exception("Nie udało się zbudować klienta Search Console.")
            return None
        return find_best_gsc_site(service, audit_url)

    def fetch_yoy_query_performance(self, credentials: Credentials, audit_url: str) -> dict:
        """Porównuje wydajność FRAZ kluczowych (`dimensions=["query"]`) - ostatnie 3
        pełne miesiące vs analogiczne 3 miesiące rok temu. Patrz
        `_fetch_yoy_dimension_performance` po pełny opis kształtu zwracanego słownika
        (klucz wiersza to tu "query")."""
        return self._fetch_yoy_dimension_performance(credentials, audit_url, dimension="query", key_name="query")

    def fetch_yoy_page_performance(self, credentials: Credentials, audit_url: str) -> dict:
        """Porównuje wydajność PODSTRON (`dimensions=["page"]`) - ostatnie 3 pełne
        miesiące vs analogiczne 3 miesiące rok temu. Patrz
        `_fetch_yoy_dimension_performance` po pełny opis kształtu zwracanego słownika
        (klucz wiersza to tu "page", z pełnym adresem URL podstrony)."""
        return self._fetch_yoy_dimension_performance(credentials, audit_url, dimension="page", key_name="page")

    def _fetch_yoy_dimension_performance(
        self, credentials: Credentials, audit_url: str, dimension: str, key_name: str
    ) -> dict:
        """Pobiera i porównuje kliknięcia pogrupowane wg `dimension` ("query" lub
        "page") dla dwóch 3-miesięcznych okresów: Okres A (ostatnie {PERIOD_MONTHS}
        pełnych miesięcy, bieżący niepełny miesiąc jest pomijany) i Okres B (te same
        miesiące kalendarzowe rok wcześniej). Usługę Search Console dopasowuje do
        `audit_url` przez `find_best_gsc_site` (patrz tam - obsługuje sc-domain:,
        http/https, z/bez www). Łączy wyniki po kluczu i wylicza deltę.

        Zwraca:
        {
            "total_clicks_current": int, "total_clicks_previous": int,
            "yoy_change_percent": float,
            "top_gainers": [{key_name: str, "clicks_current": int, "clicks_previous": int, "delta": int}, ...],
            "top_losers": [...],
        }
        """
        period_a_start, period_a_end = last_n_full_months_range(PERIOD_MONTHS)
        period_b_start, period_b_end = same_months_last_year(period_a_start, period_a_end)

        try:
            service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
            site_url = find_best_gsc_site(service, audit_url)
            if not site_url:
                return self._fallback()
            rows_current = self._query_rows(service, site_url, dimension, period_a_start, period_a_end)
            rows_previous = self._query_rows(service, site_url, dimension, period_b_start, period_b_end)
        except Exception:
            logger.exception("Błąd podczas pobierania danych Search Console (%s) dla %s.", dimension, audit_url)
            return self._fallback()

        current_by_key = {row["keys"][0]: row.get("clicks", 0) for row in rows_current if row.get("keys")}
        previous_by_key = {row["keys"][0]: row.get("clicks", 0) for row in rows_previous if row.get("keys")}

        deltas = []
        for key in set(current_by_key) | set(previous_by_key):
            clicks_current = round(current_by_key.get(key, 0))
            clicks_previous = round(previous_by_key.get(key, 0))
            deltas.append({
                key_name: key,
                "clicks_current": clicks_current,
                "clicks_previous": clicks_previous,
                "delta": clicks_current - clicks_previous,
            })

        top_gainers = sorted((d for d in deltas if d["delta"] > 0), key=lambda d: d["delta"], reverse=True)[:TOP_N]
        top_losers = sorted((d for d in deltas if d["delta"] < 0), key=lambda d: d["delta"])[:TOP_N]

        total_current = round(sum(current_by_key.values()))
        total_previous = round(sum(previous_by_key.values()))

        return {
            "total_clicks_current": total_current,
            "total_clicks_previous": total_previous,
            "yoy_change_percent": self._pct_change(total_previous, total_current) or 0.0,
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        }

    def _query_rows(self, service, site_url: str, dimension: str, start: date, end: date) -> list[dict]:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [dimension],
            "rowLimit": QUERY_ROW_LIMIT,
        }
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])

    def _pct_change(self, previous: float, current: float) -> float | None:
        if not previous:
            return None
        return round((current - previous) / previous * 100, 1)

    def _fallback(self) -> dict:
        return {
            "total_clicks_current": 0,
            "total_clicks_previous": 0,
            "yoy_change_percent": 0.0,
            "top_gainers": [],
            "top_losers": [],
        }
