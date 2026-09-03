from __future__ import annotations

import calendar
import logging
from datetime import date

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

QUERY_ROW_LIMIT = 25000
PERIOD_MONTHS = 3
TOP_N = 10


class GSCService:
    """Klient Google Search Console API (Search Analytics), autoryzowany przez OAuth
    2.0 (ten sam przepływ "Zaloguj się przez Google" co GA4, ze scope'em
    `webmasters.readonly` - patrz `settings.GA4_SCOPES`). Porównuje wydajność fraz
    kluczowych: ostatnie 3 pełne miesiące vs analogiczne 3 miesiące rok temu (3M YoY).

    Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie
    (SenutoService, GA4OAuthService): błąd komunikacji z GSC (brak dostępu, domena
    niezarejestrowana w Search Console, wygasły token) nigdy nie podnosi wyjątku na
    zewnątrz - zwracany jest bezpieczny fallback z zerowymi wartościami.
    """

    def resolve_site_url(self, credentials: Credentials, domain: str) -> str | None:
        """Znajduje URL usługi (property) Search Console odpowiadającej `domain`
        wśród usług dostępnych dla zalogowanego konta Google - dopasowuje zarówno
        usługi typu "Domain" (`sc-domain:example.com`), jak i "URL prefix"
        (`https://example.com/`, `http://www.example.com/`, ...). Zwraca None, gdy
        żadna dostępna usługa nie odpowiada tej domenie."""
        try:
            service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
            response = service.sites().list().execute()
        except Exception:
            logger.exception("Nie udało się pobrać listy usług Search Console.")
            return None

        domain = domain.lower()
        for entry in response.get("siteEntry", []):
            site_url = entry.get("siteUrl", "")
            normalized = (
                site_url.lower()
                .replace("sc-domain:", "")
                .replace("https://", "")
                .replace("http://", "")
                .rstrip("/")
            )
            if normalized == domain or normalized == f"www.{domain}":
                return site_url
        return None

    def fetch_yoy_query_performance(self, credentials: Credentials, site_url: str) -> dict:
        """Pobiera i porównuje wydajność fraz kluczowych (`dimensions=["query"]`) dla
        dwóch 3-miesięcznych okresów: Okres A (ostatnie {PERIOD_MONTHS} pełnych
        miesięcy, bieżący niepełny miesiąc jest pomijany) i Okres B (te same miesiące
        kalendarzowe rok wcześniej). Łączy wyniki po frazie i wylicza deltę kliknięć.

        Zwraca:
        {
            "total_clicks_current": int, "total_clicks_previous": int,
            "yoy_change_percent": float,
            "top_gainers": [{"query": str, "clicks_current": int, "clicks_previous": int, "delta": int}, ...],
            "top_losers": [...],
        }
        """
        period_a_start, period_a_end = self._last_n_full_months_range(PERIOD_MONTHS)
        period_b_start, period_b_end = self._same_months_last_year(period_a_start, period_a_end)

        try:
            service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
            rows_current = self._query_rows(service, site_url, period_a_start, period_a_end)
            rows_previous = self._query_rows(service, site_url, period_b_start, period_b_end)
        except Exception:
            logger.exception("Błąd podczas pobierania danych Search Console dla %s.", site_url)
            return self._fallback()

        current_by_query = {row["keys"][0]: row.get("clicks", 0) for row in rows_current if row.get("keys")}
        previous_by_query = {row["keys"][0]: row.get("clicks", 0) for row in rows_previous if row.get("keys")}

        deltas = []
        for query in set(current_by_query) | set(previous_by_query):
            clicks_current = round(current_by_query.get(query, 0))
            clicks_previous = round(previous_by_query.get(query, 0))
            deltas.append({
                "query": query,
                "clicks_current": clicks_current,
                "clicks_previous": clicks_previous,
                "delta": clicks_current - clicks_previous,
            })

        top_gainers = sorted((d for d in deltas if d["delta"] > 0), key=lambda d: d["delta"], reverse=True)[:TOP_N]
        top_losers = sorted((d for d in deltas if d["delta"] < 0), key=lambda d: d["delta"])[:TOP_N]

        total_current = round(sum(current_by_query.values()))
        total_previous = round(sum(previous_by_query.values()))

        return {
            "total_clicks_current": total_current,
            "total_clicks_previous": total_previous,
            "yoy_change_percent": self._pct_change(total_previous, total_current) or 0.0,
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        }

    def _query_rows(self, service, site_url: str, start: date, end: date) -> list[dict]:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["query"],
            "rowLimit": QUERY_ROW_LIMIT,
        }
        response = service.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return response.get("rows", [])

    def _last_n_full_months_range(self, months: int) -> tuple[date, date]:
        """Zwraca (start, end) obejmujący `months` ostatnich PEŁNYCH miesięcy
        kalendarzowych - bieżący, trwający miesiąc jest pomijany celowo (jego
        niepełne dane zaburzałyby porównanie), analogicznie do
        `GA4OAuthService._expected_year_months`."""
        today = date.today()
        year, month = today.year, today.month
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        end_year, end_month = year, month

        for _ in range(months - 1):
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        start_year, start_month = year, month

        start = date(start_year, start_month, 1)
        _, last_day = calendar.monthrange(end_year, end_month)
        end = date(end_year, end_month, last_day)
        return start, end

    def _same_months_last_year(self, start: date, end: date) -> tuple[date, date]:
        """Zwraca te same miesiące kalendarzowe co (start, end), przesunięte dokładnie
        o rok wstecz - liczone niezależnie dla każdej granicy (nie przez odjęcie 365
        dni), żeby uniknąć problemów z latami przestępnymi (29 lutego)."""
        start_b = date(start.year - 1, start.month, 1)
        _, last_day = calendar.monthrange(end.year - 1, end.month)
        end_b = date(end.year - 1, end.month, last_day)
        return start_b, end_b

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
