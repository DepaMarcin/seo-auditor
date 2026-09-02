from __future__ import annotations

import logging

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
        """Pobiera dzienną liczbę sesji z ostatnich 365 dni, BEZ filtrowania kanału -
        pogrupowaną wg `sessionDefaultChannelGroup` (Organic Search, Paid Search,
        Direct, Referral, ...). To dane wejściowe dla analizy trendów wielokanałowych
        (patrz `auditor.services.ga4_insights.analyze_channel_trends`).

        Zwraca: {"dates": ["YYYY-MM-DD", ...], "channels": {"Organic Search": [...], ...}}
        - każda tablica w "channels" ma tę samą długość co "dates" (brakujące dni w
        danym kanale są uzupełnione zerem), żeby dało się je bezpośrednio nanieść na
        wspólną oś czasu na wykresie Chart.js.
        """
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date="365daysAgo", end_date="today")],
                dimensions=[Dimension(name="date"), Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions")],
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania rocznych danych GA4 wg kanału dla property_id=%s.", property_id)
            return self._empty_channel_history()

        series_by_channel: dict[str, dict[str, int]] = {}
        all_dates: set[str] = set()
        for row in response.rows:
            raw_date = row.dimension_values[0].value
            channel = row.dimension_values[1].value
            try:
                formatted_date = self._format_ga4_date(raw_date)
                sessions_value = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz rocznych danych GA4 wg kanału: %r", raw_date)
                continue
            all_dates.add(formatted_date)
            series_by_channel.setdefault(channel, {})[formatted_date] = sessions_value

        dates = sorted(all_dates)
        channels = {
            channel: [day_values.get(day, 0) for day in dates]
            for channel, day_values in sorted(series_by_channel.items())
        }
        return {"dates": dates, "channels": channels}

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
        """Pobiera dzienną liczbę wystąpień `event_name` przypisanych do kanału
        "Organic Search" dla ostatnich `days` dni - dane wejściowe do wyliczenia
        trendu leadów/konwersji z ruchu organicznego.

        Zwraca: {"total_events": int, "history": {"dates": [...], "events": [...]}}.
        """
        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
                dimensions=[Dimension(name="date")],
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
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception(
                "Błąd podczas pobierania konwersji GA4 (event=%s) dla property_id=%s.", event_name, property_id
            )
            return self._empty_event_history()

        dates: list[str] = []
        events: list[int] = []
        for row in response.rows:
            raw_date = row.dimension_values[0].value
            try:
                formatted_date = self._format_ga4_date(raw_date)
                events_value = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz konwersji GA4: %r", raw_date)
                continue
            dates.append(formatted_date)
            events.append(events_value)

        return {
            "total_events": sum(events),
            "history": {"dates": dates, "events": events},
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

    def _fallback(self) -> dict:
        return {"total_sessions": 0, "history": {"dates": [], "sessions": []}}

    def _empty_channel_history(self) -> dict:
        return {"dates": [], "channels": {}}

    def _empty_event_history(self) -> dict:
        return {"total_events": 0, "history": {"dates": [], "events": []}}
