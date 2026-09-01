from __future__ import annotations

import logging

from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
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
                formatted_date = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
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

    def _fallback(self) -> dict:
        return {"total_sessions": 0, "history": {"dates": [], "sessions": []}}
