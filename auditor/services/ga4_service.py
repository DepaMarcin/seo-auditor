from __future__ import annotations

import logging
from datetime import date

from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

# Zgodnie z wymaganą metryką "sessionDefaultChannelGroup == 'Organic Search'" -
# jedyny kanał, który liczy się jako ruch organiczny z wyszukiwarek.
ORGANIC_CHANNEL_GROUP = "Organic Search"

# Liczba pełnych miesięcy prezentowanych na wykresach wielokanałowym i leadów.
CHANNEL_HISTORY_MONTHS = 12

# Wyłącznie te kanały pokazujemy na wykresie ruchu wielokanałowego - reszta (Referral,
# Email, Organic Social/Video/Shopping, Paid Social/Video/Other, Display, ...) jest
# zwykle marginalna wolumenowo i tylko zaciemnia wykres. Kolejność determinuje kolejność
# checkboxów/legendy nad wykresem w detail.html.
ALLOWED_CHANNELS = [
    "Organic Search",
    "Paid Search",
    "Cross-network",
    "Direct",
    "AI Assistant",
    "Unassigned",
]

_POLISH_MONTH_ABBR = {
    1: "STY", 2: "LUT", 3: "MAR", 4: "KWI", 5: "MAJ", 6: "CZE",
    7: "LIP", 8: "SIE", 9: "WRZ", 10: "PAŹ", 11: "LIS", 12: "GRU",
}


class GA4OAuthService:
    """Klient Google Analytics Data API (GA4), autoryzowany przez OAuth 2.0
    ("Zaloguj się przez Google") - pobiera dzienną historię sesji z ruchu
    organicznego dla wskazanej usługi (property) GA4.

    Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie
    (SenutoService, PageSpeedService): błąd komunikacji z GA4 nigdy nie podnosi
    wyjątku na zewnątrz - zwracany jest bezpieczny słownik z zerowymi wartościami,
    żeby nieudane połączenie z Google Analytics nie blokowało reszty audytu.
    """

    def fetch_organic_traffic(self, credentials: Credentials, property_id: str, days: int = 30) -> dict:
        """Zwraca dzienną historię sesji z ruchu organicznego dla ostatnich `days` dni:
        {"total_sessions": int, "history": {"dates": [...], "sessions": [...]}}.

        `credentials` to `google.oauth2.credentials.Credentials` uzyskane z przepływu
        OAuth 2.0 (patrz `auditor.views.ga4_callback`). `property_id` to numeryczny
        identyfikator usługi GA4 (bez prefiksu "properties/").
        """
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
                dimensions=[Dimension(name="date")],
                metrics=[Metric(name="sessions")],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="sessionDefaultChannelGroup",
                        string_filter=Filter.StringFilter(value=ORGANIC_CHANNEL_GROUP),
                    )
                ),
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania danych GA4 dla property_id=%s.", property_id)
            return self._fallback()

        dates: list[str] = []
        sessions: list[int] = []
        for row in response.rows:
            raw_date = row.dimension_values[0].value  # format GA4: "YYYYMMDD"
            try:
                formatted_date = self._format_ga4_date(raw_date)
                sessions_value = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz odpowiedzi GA4: %r", raw_date)
                continue
            dates.append(formatted_date)
            sessions.append(sessions_value)

        return {
            "total_sessions": sum(sessions),
            "history": {"dates": dates, "sessions": sessions},
        }

    def fetch_yearly_channel_data(self, credentials: Credentials, property_id: str) -> dict:
        """Pobiera MIESIĘCZNĄ liczbę sesji dla ostatnich {CHANNEL_HISTORY_MONTHS} pełnych
        miesięcy - GA4 sam agreguje dane wg wymiaru "yearMonth" (serwerowo, bez potrzeby
        sumowania dni po stronie Pythona), pogrupowaną wg `sessionDefaultChannelGroup` i
        ograniczoną do kanałów z `ALLOWED_CHANNELS` (pozostałe, marginalne kanały są
        pomijane, żeby wykres pozostał czytelny). To dane wejściowe dla analizy trendów
        wielokanałowych (patrz `auditor.services.ga4_insights.analyze_channel_trends`).

        Zwraca: {"months": ["WRZ 2025", ..., "SIE 2026"], "channels": {"Organic Search": [...], ...}}
        - każda tablica w "channels" ma dokładnie {CHANNEL_HISTORY_MONTHS} elementów
        (miesiące bez żadnych sesji w danym kanale są uzupełnione zerem), a klucze
        "channels" zawsze obejmują wszystkie `ALLOWED_CHANNELS` w tej samej kolejności -
        nawet jeśli dany kanał nie wystąpił w danych ani razu.
        """
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date="365daysAgo", end_date="today")],
                dimensions=[Dimension(name="yearMonth"), Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions")],
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="yearMonth"))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania rocznych danych GA4 wg kanału dla property_id=%s.", property_id)
            return self._empty_channel_history()

        allowed = set(ALLOWED_CHANNELS)
        series_by_channel: dict[str, dict[str, int]] = {}
        for row in response.rows:
            channel = row.dimension_values[1].value
            if channel not in allowed:
                continue
            raw_month = row.dimension_values[0].value  # format GA4: "YYYYMM"
            try:
                sessions_value = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz rocznych danych GA4 wg kanału: %r", raw_month)
                continue
            series_by_channel.setdefault(channel, {})[raw_month] = sessions_value

        expected_months = self._expected_year_months(CHANNEL_HISTORY_MONTHS)
        months = [self._format_year_month(m) for m in expected_months]
        channels = {
            channel: [series_by_channel.get(channel, {}).get(m, 0) for m in expected_months]
            for channel in ALLOWED_CHANNELS
        }
        return {"months": months, "channels": channels}

    def get_available_events(self, credentials: Credentials, property_id: str) -> list[str]:
        """Zwraca listę unikalnych nazw zdarzeń (`eventName`) zarejestrowanych w GA4 w
        ostatnich 90 dniach, posortowaną wg popularności (malejąco) - do wyboru
        zdarzenia reprezentującego lead/konwersję (patrz `Audit.ga4_selected_lead_event`).
        W razie błędu API zwraca pustą listę (formularz wyboru zdarzenia po prostu
        będzie pusty, co nie blokuje reszty audytu)."""
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date="90daysAgo", end_date="today")],
                dimensions=[Dimension(name="eventName")],
                metrics=[Metric(name="eventCount")],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania listy zdarzeń GA4 dla property_id=%s.", property_id)
            return []

        return [row.dimension_values[0].value for row in response.rows if row.dimension_values[0].value]

    def fetch_event_conversions(
        self, credentials: Credentials, property_id: str, event_name: str, days: int = 365
    ) -> dict:
        """Pobiera MIESIĘCZNĄ liczbę wystąpień `event_name` przypisanych do kanału
        "Organic Search" dla ostatnich {CHANNEL_HISTORY_MONTHS} pełnych miesięcy (GA4
        agreguje serwerowo wg wymiaru "yearMonth") - dane wejściowe do wyliczenia trendu
        leadów/konwersji z ruchu organicznego (osobny wykres pod głównym wykresem
        kanałów w `detail.html`).

        Zwraca: {"total_events": int, "history": {"months": ["WRZ 2025", ...], "events": [...]}}
        - "events" ma zawsze dokładnie {CHANNEL_HISTORY_MONTHS} elementów, z zerami dla
        miesięcy bez ani jednego wystąpienia zdarzenia (GA4 nie zwraca dla nich wiersza).
        """
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
                dimensions=[Dimension(name="yearMonth")],
                metrics=[Metric(name="eventCount")],
                dimension_filter=FilterExpression(
                    and_group=FilterExpressionList(
                        expressions=[
                            FilterExpression(
                                filter=Filter(
                                    field_name="sessionDefaultChannelGroup",
                                    string_filter=Filter.StringFilter(value=ORGANIC_CHANNEL_GROUP),
                                )
                            ),
                            FilterExpression(
                                filter=Filter(
                                    field_name="eventName",
                                    string_filter=Filter.StringFilter(value=event_name),
                                )
                            ),
                        ]
                    )
                ),
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="yearMonth"))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception(
                "Błąd podczas pobierania konwersji GA4 (event=%s) dla property_id=%s.", event_name, property_id
            )
            return self._empty_event_history()

        by_month: dict[str, int] = {}
        for row in response.rows:
            raw_month = row.dimension_values[0].value
            try:
                by_month[raw_month] = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz konwersji GA4: %r", raw_month)
                continue

        expected_months = self._expected_year_months(CHANNEL_HISTORY_MONTHS)
        months = [self._format_year_month(m) for m in expected_months]
        events = [by_month.get(m, 0) for m in expected_months]

        return {
            "total_events": sum(events),
            "history": {"months": months, "events": events},
        }

    def list_accessible_properties(self, credentials: Credentials) -> list[dict]:
        """Zwraca listę wszystkich usług (properties) GA4 dostępnych dla zalogowanego
        konta Google, jako [{"property_id": "312345678", "display_name": "enova.pl"}].

        Używane, gdy konto ma dostęp do wielu usług GA4 i backend nie może się
        domyślić, która z nich odpowiada audytowanej domenie - wynik jest prezentowany
        użytkownikowi do ręcznego wyboru (patrz `auditor.views.select_ga4_property`).
        Korzysta z Google Analytics Admin API (`AccountSummaries`), które w jednym
        zapytaniu zwraca właściwości pogrupowane wg wszystkich kont dostępnych dla
        zalogowanego użytkownika - w razie błędu zwraca pustą listę (nie przerywa
        przepływu logowania).
        """
        try:
            client = AnalyticsAdminServiceClient(credentials=credentials)
            properties: list[dict] = []
            for account_summary in client.list_account_summaries():
                for property_summary in account_summary.property_summaries:
                    # `property_summary.property` ma postać "properties/312345678".
                    property_id = property_summary.property.rsplit("/", 1)[-1]
                    properties.append({
                        "property_id": property_id,
                        "display_name": property_summary.display_name,
                    })
            return properties
        except Exception:
            logger.exception("Nie udało się pobrać listy usług GA4 (AccountSummaries) z Google Admin API.")
            return []

    def build_credentials_from_refresh_token(
        self, refresh_token: str, client_id: str, client_secret: str
    ) -> Credentials:
        """Odtwarza `Credentials` z zapisanego wcześniej `refresh_token` (bez
        konieczności ponownego przechodzenia przez ekran zgody Google) - do
        cyklicznego odświeżania danych GA4 dla już połączonego audytu."""
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )

    def _format_ga4_date(self, raw_date: str) -> str:
        """Konwertuje datę w formacie GA4 ("YYYYMMDD") na ISO ("YYYY-MM-DD")."""
        return f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"

    def _format_year_month(self, raw_year_month: str) -> str:
        """Konwertuje miesiąc w formacie GA4 ("YYYYMM") na czytelną polską etykietę,
        np. "202509" -> "WRZ 2025"."""
        year, month = raw_year_month[:4], int(raw_year_month[4:6])
        return f"{_POLISH_MONTH_ABBR.get(month, raw_year_month[4:6])} {year}"

    def _expected_year_months(self, count: int) -> list[str]:
        """Zwraca `count` kolejnych kluczy "YYYYMM" (chronologicznie), kończących się na
        OSTATNIM PEŁNYM miesiącu (bieżący, trwający miesiąc jest pomijany celowo - jego
        niepełne dane sztucznie zaniżałyby ostatni punkt wykresu i psuły porównania m/m,
        np. 2 dni września potraktowane jak cały miesiąc). Gwarantuje pełną, wyrównaną
        oś 12 miesięcy nawet gdy w którymś miesiącu GA4 nie zwróciło żadnego wiersza."""
        today = date.today()
        year, month = today.year, today.month
        month -= 1
        if month == 0:
            month = 12
            year -= 1

        keys = []
        for _ in range(count):
            keys.append(f"{year:04d}{month:02d}")
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        return list(reversed(keys))

    def _fallback(self) -> dict:
        return {"total_sessions": 0, "history": {"dates": [], "sessions": []}}

    def _empty_channel_history(self) -> dict:
        return {"months": [], "channels": {}}

    def _empty_event_history(self) -> dict:
        return {"total_events": 0, "history": {"months": [], "events": []}}
