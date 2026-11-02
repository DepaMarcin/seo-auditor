from __future__ import annotations

import hashlib
import logging
from datetime import date
from urllib.parse import urlparse

import google_auth_httplib2
import httplib2
from django.conf import settings
from django.core.cache import cache
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .date_ranges import default_range, previous_year_range

logger = logging.getLogger(__name__)

QUERY_ROW_LIMIT = 25000
TOP_N = 10

# googleapiclient bez jawnego limitu korzysta z domyślnego timeoutu gniazda (który
# potrafi być bardzo długi lub nieskończony) - zawieszone zapytanie do Search Console
# blokowałoby wtedy cały wątek audytu.
GSC_HTTP_TIMEOUT_SECONDS = 30


def find_best_gsc_site(service, audit_url: str) -> str | None:
    """Usługa Search Console odpowiadająca `audit_url`, wybrana na podstawie RUCHU.

    Jedno miejsce na tę decyzję: wcześniej stała tu druga, niezależna implementacja
    dopasowania po postaci adresu, a `auditor.services.google_api` miała własną,
    opartą na pomiarze wyświetleń. Dwa algorytmy wybierały różne usługi, więc
    automat w tle pobierał dane z innej witryny niż wskazywał formularz w interfejsie.

    Tamta kolejność miała też wadę merytoryczną: krok "dokładny adres audytu"
    wygrywał z wariantem "www", a dla motivationdirect.pl prefiks bez "www" miał
    1 kliknięcie, podczas gdy ruch (1248 kliknięć) zbierał się na wariancie z "www".

    Zwraca `siteUrl` albo None, gdy żadna usługa nie pasuje lub nie udało się
    pobrać listy.
    """
    from .google_api import audit_domain, resolve_gsc_site

    domain = audit_domain(audit_url)
    if not domain:
        logger.warning("[GSC] Nie udało się wyodrębnić domeny z adresu audytu: %r.", audit_url)
        return None

    try:
        response = service.sites().list().execute() or {}
    except Exception:
        logger.exception("[GSC] Nie udało się pobrać listy usług Search Console (sites().list()).")
        return None

    witryny = [
        {"site_url": wpis.get("siteUrl", "")}
        for wpis in response.get("siteEntry", [])
        if wpis.get("siteUrl")
    ]
    if not witryny:
        logger.info(
            "[GSC] Konto Google nie ma dostępu do żadnej usługi Search Console "
            "(domena audytu: %s).", domain,
        )
        return None

    wybrana = resolve_gsc_site(service, witryny, domain)
    if not wybrana:
        logger.info(
            "[GSC] Nie znaleziono usługi Search Console dla domeny %s. Dostępne usługi: %s",
            domain, [w["site_url"] for w in witryny],
        )
        return None

    logger.info("[GSC] Dla domeny %s wybrano usługę %r.", domain, wybrana)
    return wybrana


class GSCService:
    """Klient Google Search Console API (Search Analytics), autoryzowany przez OAuth
    2.0 (ten sam przepływ "Zaloguj się przez Google" co GA4, ze scope'em
    `webmasters.readonly` - patrz `settings.GA4_SCOPES`). Porównuje wydajność fraz
    kluczowych oraz podstron dla wybranego zakresu dat vs ten sam zakres rok wcześniej
    (bez podanego zakresu: ostatnie 28 pełnych dni, kończąc na wczoraj).

    Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie
    (SenutoService, GA4OAuthService): błąd komunikacji z GSC (brak dostępu, domena
    niezarejestrowana w Search Console, wygasły token) nigdy nie podnosi wyjątku na
    zewnątrz - zwracany jest bezpieczny fallback z zerowymi wartościami.
    """

    def _build_service(self, credentials: Credentials):
        """Buduje klienta Search Console z JAWNYM limitem czasu żądania."""
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=GSC_HTTP_TIMEOUT_SECONDS)
        )
        return build("searchconsole", "v1", http=authorized_http, cache_discovery=False)

    def resolve_site_url(self, credentials: Credentials, audit_url: str) -> str | None:
        """Wygodny skrót: buduje klienta Search Console i zwraca `find_best_gsc_site`
        dla `audit_url` - do sprawdzenia, czy/która usługa GSC pasuje do domeny, bez
        pobierania właściwych danych o kliknięciach."""
        try:
            service = self._build_service(credentials)
        except Exception:
            logger.exception("Nie udało się zbudować klienta Search Console.")
            return None
        return find_best_gsc_site(service, audit_url)

    def fetch_yoy_query_performance(
        self,
        credentials: Credentials,
        audit_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        site_url: str | None = None,
    ) -> dict:
        """Porównuje wydajność FRAZ kluczowych (`dimensions=["query"]`) w podanym
        zakresie vs ten sam zakres rok wcześniej. Bez podanego zakresu: ostatnie 3
        pełne miesiące. Patrz `_fetch_yoy_dimension_performance` po pełny opis kształtu
        zwracanego słownika (klucz wiersza to tu "query")."""
        return self._fetch_yoy_dimension_performance(
            credentials, audit_url, dimension="query", key_name="query",
            start_date=start_date, end_date=end_date, site_url=site_url,
        )

    def fetch_yoy_page_performance(
        self,
        credentials: Credentials,
        audit_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        site_url: str | None = None,
    ) -> dict:
        """Porównuje wydajność PODSTRON (`dimensions=["page"]`) w podanym zakresie vs
        ten sam zakres rok wcześniej. Bez podanego zakresu: ostatnie 3 pełne miesiące.
        Patrz `_fetch_yoy_dimension_performance` po pełny opis kształtu zwracanego
        słownika (klucz wiersza to tu "page", z pełnym adresem URL podstrony)."""
        return self._fetch_yoy_dimension_performance(
            credentials, audit_url, dimension="page", key_name="page",
            start_date=start_date, end_date=end_date, site_url=site_url,
        )

    def _fetch_yoy_dimension_performance(
        self,
        credentials: Credentials,
        audit_url: str,
        dimension: str,
        key_name: str,
        start_date: date | None = None,
        end_date: date | None = None,
        site_url: str | None = None,
    ) -> dict:
        """Pobiera i porównuje kliknięcia pogrupowane wg `dimension` ("query" lub
        "page") dla dwóch okresów: Okresu A (zakres podany przez `start_date`/`end_date`,
        a bez niego - domyślne okno z `date_ranges.default_range`, czyli pełne dni
        kończące się wczoraj) i Okresu
        B (ten sam zakres przesunięty o rok wstecz). Usługę Search Console dopasowuje do
        `audit_url` przez `find_best_gsc_site` (patrz tam - obsługuje sc-domain:,
        http/https, z/bez www).

        UWAGA: "total_clicks_current"/"total_clicks_previous" pochodzą z OSOBNEGO
        zapytania bez wymiarów (`dimensions=[]`, patrz `_fetch_site_totals`), a NIE z
        sumy wierszy pogrupowanych wg `dimension` - Google Search Console ukrywa
        (nie zwraca w ogóle) rzadkie/anonimowe frazy w rozbiciu `dimensions=["query"]`
        ze względów prywatności, więc suma samych wierszy zaniżałaby rzeczywisty ruch
        całej witryny (np. pokazywałaby ~500 zamiast realnych ~1500 kliknięć).
        Rozbicie wg `dimension` służy WYŁĄCZNIE do wyliczenia TOP 10 Wzrostów/Spadków.

        Zwraca:
        {
            "total_clicks_current": int, "total_clicks_previous": int,
            "yoy_change_percent": float,
            "top_gainers": [{key_name: str, "clicks_current": int, "clicks_previous": int, "delta": int}, ...],
            "top_losers": [...],
        }
        """
        if start_date and end_date:
            period_a_start, period_a_end = start_date, end_date
            period_b_start, period_b_end = previous_year_range(start_date, end_date)
        else:
            # Ten sam domyślny zakres co kafelek GA4: 28 pełnych dni kończących się
            # wczoraj. Wcześniej stały tu 3 pełne miesiące kalendarzowe (~92 dni),
            # więc kafelki GA4 i Search Console pokazywały liczby z RÓŻNYCH okresów
            # i nie dawały się ze sobą porównać.
            period_a_start, period_a_end = default_range()
            period_b_start, period_b_end = previous_year_range(period_a_start, period_a_end)

        # GSC publikuje dane z 2-3 dniowym opóźnieniem, więc częstsze odpytywanie nie
        # przyniesie nowych liczb - a każde zapytanie to 4 wywołania API. Klucz MUSI
        # zawierać zakres dat, inaczej po zmianie zakresu w interfejsie wróciłyby
        # zbuforowane dane poprzedniego okresu.
        site_digest = hashlib.sha256((site_url or audit_url).encode()).hexdigest()[:32]
        cache_key = (
            f"gsc:{dimension}:{site_digest}:{period_a_start.isoformat()}:{period_a_end.isoformat()}"
        )
        cached = cache.get(cache_key)
        if cached is not None:
            logger.info("Search Console: dane (%s) dla %s pobrane z cache.", dimension, audit_url)
            return cached

        try:
            service = self._build_service(credentials)
            # Witryna wskazana wprost w formularzu WYGRYWA z automatem. Wcześniej
            # każdy wywołujący podawał tylko adres audytu, a serwis wybierał usługę
            # sam - wybór użytkownika był po cichu ignorowany i dla
            # motivationdirect.pl kafelek pokazywał 1 kliknięcie zamiast 1248.
            site_url = site_url or find_best_gsc_site(service, audit_url)
            if not site_url:
                return self._fallback()
            logger.info("[GSC] Dane (%s) pobieram z usługi %s.", dimension, site_url)
            rows_current = self._query_rows(service, site_url, dimension, period_a_start, period_a_end)
            rows_previous = self._query_rows(service, site_url, dimension, period_b_start, period_b_end)
            totals_current = self._fetch_site_totals(service, site_url, period_a_start, period_a_end)
            totals_previous = self._fetch_site_totals(service, site_url, period_b_start, period_b_end)
        except Exception:
            logger.exception("Błąd podczas pobierania danych Search Console (%s) dla %s.", dimension, audit_url)
            return self._fallback()

        # Zachowujemy CAŁY wiersz z API, nie same kliknięcia - Search Console zwraca przy
        # okazji wyświetlenia, CTR i średnią pozycję, a te trafiają do zakładki "Ruch
        # i Widoczność" w eksporcie (auditor.services.exporter). Pobranie ich osobno
        # oznaczałoby dodatkowe zapytania do API po dane, które już mamy w odpowiedzi.
        current_by_key = {row["keys"][0]: row for row in rows_current if row.get("keys")}
        previous_by_key = {row["keys"][0]: row for row in rows_previous if row.get("keys")}

        deltas = []
        for key in set(current_by_key) | set(previous_by_key):
            current_row = current_by_key.get(key, {})
            previous_row = previous_by_key.get(key, {})
            clicks_current = round(current_row.get("clicks", 0))
            clicks_previous = round(previous_row.get("clicks", 0))
            deltas.append({
                key_name: key,
                "clicks_current": clicks_current,
                "clicks_previous": clicks_previous,
                "delta": clicks_current - clicks_previous,
                "impressions_current": round(current_row.get("impressions", 0)),
                "impressions_previous": round(previous_row.get("impressions", 0)),
                # GSC podaje CTR jako ułamek (0.0521), a w raporcie czytelniejszy jest procent.
                "ctr_current": round(current_row.get("ctr", 0) * 100, 2),
                "ctr_previous": round(previous_row.get("ctr", 0) * 100, 2),
                "position_current": round(current_row.get("position", 0), 1),
                "position_previous": round(previous_row.get("position", 0), 1),
            })

        top_gainers = sorted((d for d in deltas if d["delta"] > 0), key=lambda d: d["delta"], reverse=True)[:TOP_N]
        top_losers = sorted((d for d in deltas if d["delta"] < 0), key=lambda d: d["delta"])[:TOP_N]

        total_current = totals_current["clicks"]
        total_previous = totals_previous["clicks"]
        logger.info(
            "[GSC] Sumy witrynowe (%s) dla %s: obecnie=%s kliknięć / %s wyświetleń, "
            "rok temu=%s kliknięć / %s wyświetleń.",
            dimension, site_url, total_current, totals_current["impressions"],
            total_previous, totals_previous["impressions"],
        )

        result = {
            "total_clicks_current": total_current,
            "total_clicks_previous": total_previous,
            "yoy_change_percent": self._pct_change(total_previous, total_current) or 0.0,
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        }
        cache.set(cache_key, result, getattr(settings, "CACHE_TTL_GSC", 12 * 3600))
        return result

    def _query_rows(self, service, site_url: str, dimension: str, start: date, end: date) -> list[dict]:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [dimension],
            "rowLimit": QUERY_ROW_LIMIT,
            # Oba poniżej to obecne wartości DOMYŚLNE API, podane jawnie: raport ma
            # pokazywać dokładnie to, co karta "Wyniki w wyszukiwarce" w Search
            # Console. "web" wyklucza Discover i Wiadomości, a "final" bierze tylko
            # dane kompletne - bez nich porównanie rok do roku mieszałoby okres
            # domknięty z ostatnimi dniami, które Google jeszcze uzupełnia.
            "type": "web",
            "dataState": "final",
        }
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])

    def _fetch_site_totals(self, service, site_url: str, start: date, end: date) -> dict:
        """Pobiera DOKŁADNĄ, całkowitą liczbę kliknięć i wyświetleń dla całej usługi w
        danym okresie - zapytanie BEZ żadnych wymiarów (`dimensions=[]`) zwraca jeden
        zagregowany wiersz obejmujący cały ruch witryny, w tym anonimowe/rzadkie frazy
        pomijane przez Google w rozbiciu `dimensions=["query"]`. To jedyne rzetelne
        źródło nagłówkowej liczby kliknięć (patrz `_fetch_yoy_dimension_performance`)."""
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [],
            # Te same wartości co w `_query_rows` - inaczej suma nagłówkowa
            # obejmowałaby inny zakres niż rozbicie pod nią.
            "type": "web",
            "dataState": "final",
        }
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        rows = response.get("rows", [])
        if not rows:
            return {"clicks": 0, "impressions": 0}
        row = rows[0]
        return {
            "clicks": round(row.get("clicks", 0)),
            "impressions": round(row.get("impressions", 0)),
        }

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
