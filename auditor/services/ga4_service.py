from __future__ import annotations

import hashlib
import logging
from datetime import date

from django.conf import settings
from django.core.cache import cache
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

from .date_ranges import (
    MONTHLY_GRANULARITY_THRESHOLD_DAYS,
    default_range,
    expected_days,
    expected_months_between,
    expected_year_months,
    format_day_label,
    format_month_label,
    last_n_full_months_range,
    previous_year_range,
    same_months_last_year,
    use_monthly_granularity,
)

logger = logging.getLogger(__name__)

# Zgodnie z wymaganą metryką "sessionDefaultChannelGroup == 'Organic Search'" -
# jedyny kanał, który liczy się jako ruch organiczny z wyszukiwarek.
ORGANIC_CHANNEL_GROUP = "Organic Search"

# Liczba pełnych miesięcy prezentowanych na wykresach wielokanałowym i leadów.
CHANNEL_HISTORY_MONTHS = 12

# Liczba miesięcy porównania rok-do-roku dla "Automatycznych Wniosków SEO"
# (ostatnie 3 pełne miesiące vs analogiczne 3 miesiące rok temu).
YOY_COMPARISON_MONTHS = 3

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

class GA4OAuthService:
    """Klient Google Analytics Data API (GA4), autoryzowany przez OAuth 2.0
    ("Zaloguj się przez Google") - pobiera dzienną historię sesji z ruchu
    organicznego dla wskazanej usługi (property) GA4.

    Zgodnie z konwencją pozostałych integracji zewnętrznych w tym projekcie
    (SenutoService, PageSpeedService): błąd komunikacji z GA4 nigdy nie podnosi
    wyjątku na zewnątrz - zwracany jest bezpieczny słownik z zerowymi wartościami,
    żeby nieudane połączenie z Google Analytics nie blokowało reszty audytu.
    """

    def fetch_organic_traffic(
        self,
        credentials: Credentials,
        property_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
        days: int = 30,
    ) -> dict:
        """Zwraca historię sesji z ruchu organicznego dla wskazanego zakresu dat:
        {"total_sessions": int, "history": {"dates": [...], "sessions": [...]}}.

        Zakres podaje się przez `start_date`/`end_date`; pominięcie obu oznacza
        ostatnie `days` dni (zachowanie domyślne przy pierwszym podłączeniu usługi).

        Ziarnistość dobierana jest automatycznie: dla zakresów do
        {MONTHLY_GRANULARITY_THRESHOLD_DAYS} dni dane są dzienne (wymiar "date"), dla
        dłuższych - miesięczne (wymiar "yearMonth", agregacja po stronie GA4). Oś jest
        zawsze wyrównana do pełnego zakresu: okresy bez sesji dostają zero, zamiast
        znikać z wykresu.

        `credentials` to `google.oauth2.credentials.Credentials` uzyskane z przepływu
        OAuth 2.0 (patrz `auditor.views.ga4_callback`). `property_id` to numeryczny
        identyfikator usługi GA4 (bez prefiksu "properties/").
        """
        start_date, end_date = self._resolve_range(start_date, end_date, days)
        monthly = use_monthly_granularity(start_date, end_date)
        dimension = "yearMonth" if monthly else "date"

        cache_key = self._cache_key("traffic", property_id, start_date, end_date)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=start_date.isoformat(), end_date=end_date.isoformat())],
                dimensions=[Dimension(name=dimension)],
                metrics=[Metric(name="sessions")],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="sessionDefaultChannelGroup",
                        string_filter=Filter.StringFilter(value=ORGANIC_CHANNEL_GROUP),
                    )
                ),
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=dimension))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania danych GA4 dla property_id=%s.", property_id)
            return self._fallback()

        by_key: dict[str, int] = {}
        for row in response.rows:
            raw_key = row.dimension_values[0].value  # "YYYYMMDD" albo "YYYYMM"
            try:
                by_key[raw_key] = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz odpowiedzi GA4: %r", raw_key)
                continue

        if monthly:
            expected = expected_months_between(start_date, end_date)
            labels = [format_month_label(key) for key in expected]
        else:
            expected = expected_days(start_date, end_date)
            labels = [format_day_label(key) for key in expected]
        sessions = [by_key.get(key, 0) for key in expected]

        result = {
            "total_sessions": sum(sessions),
            "granularity": "month" if monthly else "day",
            "history": {"dates": labels, "sessions": sessions},
        }
        cache.set(cache_key, result, getattr(settings, "CACHE_TTL_GA4_DATA", 3600))
        return result

    def _resolve_range(
        self, start_date: date | None, end_date: date | None, days: int
    ) -> tuple[date, date]:
        """Uzupełnia brakujące granice zakresu domyślnym oknem ostatnich `days` dni."""
        if start_date and end_date:
            return start_date, end_date
        return default_range(days)

    def _cache_key(self, kind: str, property_id: str, start_date: date, end_date: date, extra: str = "") -> str:
        """Klucz cache ZAWSZE zawiera zakres dat - bez tego zmiana zakresu w interfejsie
        dostawałaby z powrotem dane poprzedniego okresu."""
        suffix = f":{hashlib.sha256(extra.encode()).hexdigest()[:16]}" if extra else ""
        return f"ga4:{kind}:{property_id}:{start_date.isoformat()}:{end_date.isoformat()}{suffix}"

    def fetch_channel_history(
        self,
        credentials: Credentials,
        property_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Pobiera liczbę sesji w podanym zakresie, pogrupowaną wg
        `sessionDefaultChannelGroup` i ograniczoną do kanałów z `ALLOWED_CHANNELS`
        (pozostałe, marginalne kanały są pomijane, żeby wykres pozostał czytelny).
        To dane wejściowe dla analizy trendów wielokanałowych (patrz
        `auditor.services.ga4_insights.analyze_channel_trends`).

        Bez podanego zakresu zwraca ostatnie {CHANNEL_HISTORY_MONTHS} pełnych miesięcy
        (zachowanie sprzed wprowadzenia dynamicznych zakresów, używane przy pierwszym
        podłączeniu usługi). Ziarnistość - jak w `fetch_organic_traffic` - dobierana
        automatycznie: dzienna dla krótkich zakresów, miesięczna dla długich.

        Zwraca: {"months": ["WRZ 2025", ...], "channels": {"Organic Search": [...], ...}}
        - każda tablica ma tyle elementów, ile etykiet w "months" (okresy bez sesji w
        danym kanale są uzupełnione zerem), a klucze "channels" zawsze obejmują
        wszystkie `ALLOWED_CHANNELS` w tej samej kolejności - nawet jeśli dany kanał
        nie wystąpił w danych ani razu.
        """
        if start_date and end_date:
            monthly = use_monthly_granularity(start_date, end_date)
            expected = (
                expected_months_between(start_date, end_date) if monthly
                else expected_days(start_date, end_date)
            )
            range_start, range_end = start_date, end_date
        else:
            monthly = True
            expected = expected_year_months(CHANNEL_HISTORY_MONTHS)
            range_start, range_end = last_n_full_months_range(CHANNEL_HISTORY_MONTHS)

        dimension = "yearMonth" if monthly else "date"
        cache_key = self._cache_key("channels", property_id, range_start, range_end)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=range_start.isoformat(), end_date=range_end.isoformat())],
                dimensions=[Dimension(name=dimension), Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions")],
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=dimension))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception("Błąd podczas pobierania danych GA4 wg kanału dla property_id=%s.", property_id)
            return self._empty_channel_history()

        allowed = set(ALLOWED_CHANNELS)
        series_by_channel: dict[str, dict[str, int]] = {}
        for row in response.rows:
            channel = row.dimension_values[1].value
            if channel not in allowed:
                continue
            raw_key = row.dimension_values[0].value
            try:
                sessions_value = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz danych GA4 wg kanału: %r", raw_key)
                continue
            series_by_channel.setdefault(channel, {})[raw_key] = sessions_value

        labels = [
            format_month_label(key) if monthly else format_day_label(key)
            for key in expected
        ]
        channels = {
            channel: [series_by_channel.get(channel, {}).get(key, 0) for key in expected]
            for channel in ALLOWED_CHANNELS
        }
        result = {"months": labels, "channels": channels}
        cache.set(cache_key, result, getattr(settings, "CACHE_TTL_GA4_DATA", 3600))
        return result

    def fetch_yearly_channel_data(self, credentials: Credentials, property_id: str) -> dict:
        """Zgodność wsteczna: 12 pełnych miesięcy danych wielokanałowych.

        Cienka nakładka na `fetch_channel_history` bez zakresu - używana przy
        pierwszym podłączeniu usługi GA4 (`AuditService._refresh_ga4_insights`).
        """
        return self.fetch_channel_history(credentials, property_id)

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
        self,
        credentials: Credentials,
        property_id: str,
        event_name: str,
        start_date: date | None = None,
        end_date: date | None = None,
        days: int = 365,
    ) -> dict:
        """Pobiera liczbę wystąpień `event_name` przypisanych do kanału "Organic Search"
        w podanym zakresie - dane wejściowe do wyliczenia trendu leadów/konwersji z ruchu
        organicznego (osobny wykres pod głównym wykresem kanałów w `detail.html`).

        Bez podanego zakresu zwraca ostatnie {CHANNEL_HISTORY_MONTHS} pełnych miesięcy.
        Ziarnistość dobierana automatycznie, oś wyrównana do pełnego zakresu (okresy bez
        ani jednego wystąpienia zdarzenia dostają zero - GA4 nie zwraca dla nich wiersza).

        Zwraca: {"total_events": int, "history": {"months": ["WRZ 2025", ...], "events": [...]}}
        """
        if start_date and end_date:
            monthly = use_monthly_granularity(start_date, end_date)
            expected = (
                expected_months_between(start_date, end_date) if monthly
                else expected_days(start_date, end_date)
            )
            range_start, range_end = start_date, end_date
        else:
            monthly = True
            expected = expected_year_months(CHANNEL_HISTORY_MONTHS)
            range_start, range_end = last_n_full_months_range(CHANNEL_HISTORY_MONTHS)

        dimension = "yearMonth" if monthly else "date"
        cache_key = self._cache_key("events", property_id, range_start, range_end, extra=event_name)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=range_start.isoformat(), end_date=range_end.isoformat())],
                dimensions=[Dimension(name=dimension)],
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
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name=dimension))],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception(
                "Błąd podczas pobierania konwersji GA4 (event=%s) dla property_id=%s.", event_name, property_id
            )
            return self._empty_event_history()

        by_key: dict[str, int] = {}
        for row in response.rows:
            raw_key = row.dimension_values[0].value
            try:
                by_key[raw_key] = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                logger.warning("Pominięto nieprawidłowy wiersz konwersji GA4: %r", raw_key)
                continue

        labels = [
            format_month_label(key) if monthly else format_day_label(key)
            for key in expected
        ]
        events = [by_key.get(key, 0) for key in expected]

        result = {
            "total_events": sum(events),
            "history": {"months": labels, "events": events},
        }
        cache.set(cache_key, result, getattr(settings, "CACHE_TTL_GA4_DATA", 3600))
        return result

    def fetch_channel_totals(
        self, credentials: Credentials, property_id: str, start_date: date, end_date: date
    ) -> dict[str, int]:
        """Zwraca sumę sesji wg kanału (ograniczoną do `ALLOWED_CHANNELS`) dla
        wskazanego zakresu dat - JEDNO zapytanie bez wymiaru dni/miesięcy, więc GA4
        zwraca od razu zagregowany total per kanał dla całego okresu. Używane do
        porównań rok-do-roku (patrz `fetch_yoy_summary`), niezależnie od danych
        szeregu czasowego do wykresu (`fetch_channel_history`)."""
        cache_key = self._cache_key("channel_totals", property_id, start_date, end_date)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=start_date.isoformat(), end_date=end_date.isoformat())],
                dimensions=[Dimension(name="sessionDefaultChannelGroup")],
                metrics=[Metric(name="sessions")],
            )
            response = client.run_report(request)
        except Exception:
            logger.exception(
                "Błąd podczas pobierania sumarycznych sesji GA4 wg kanału dla property_id=%s (%s - %s).",
                property_id, start_date, end_date,
            )
            return {}

        allowed = set(ALLOWED_CHANNELS)
        totals: dict[str, int] = {}
        for row in response.rows:
            channel = row.dimension_values[0].value
            if channel not in allowed:
                continue
            try:
                totals[channel] = int(row.metric_values[0].value)
            except (IndexError, ValueError):
                continue

        cache.set(cache_key, totals, getattr(settings, "CACHE_TTL_GA4_DATA", 3600))
        return totals

    def fetch_event_total(
        self, credentials: Credentials, property_id: str, event_name: str, start_date: date, end_date: date
    ) -> int:
        """Zwraca łączną liczbę wystąpień `event_name` przypisanych do kanału Organic
        Search dla wskazanego zakresu dat - jedna zagregowana wartość, bez podziału
        na dni/miesiące. Używane do porównania rok-do-roku trendu leadów (patrz
        `fetch_yoy_summary`)."""
        cache_key = self._cache_key("event_total", property_id, start_date, end_date, extra=event_name)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            client = BetaAnalyticsDataClient(credentials=credentials)
            request = RunReportRequest(
                property=f"properties/{property_id}",
                date_ranges=[DateRange(start_date=start_date.isoformat(), end_date=end_date.isoformat())],
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
            )
            response = client.run_report(request)
        except Exception:
            logger.exception(
                "Błąd podczas pobierania sumy zdarzeń GA4 (event=%s) dla property_id=%s (%s - %s).",
                event_name, property_id, start_date, end_date,
            )
            return 0

        try:
            total = int(response.rows[0].metric_values[0].value) if response.rows else 0
        except (IndexError, ValueError):
            total = 0

        cache.set(cache_key, total, getattr(settings, "CACHE_TTL_GA4_DATA", 3600))
        return total

    def fetch_yoy_summary(
        self,
        credentials: Credentials,
        property_id: str,
        start_date: date,
        end_date: date,
        lead_event_name: str | None = None,
    ) -> dict:
        """Zagregowane porównanie rok-do-roku dla DOWOLNEGO zakresu dat: wybrany okres
        vs ten sam okres przesunięty o rok wstecz (`previous_year_range`). Zwraca sesje
        wg kanału oraz - opcjonalnie - liczbę wybranego zdarzenia lead/konwersja z ruchu
        organicznego. Dane wejściowe dla `auditor.services.ga4_insights.analyze_channel_trends`.

        Zwraca:
        {
            "channels": {"current": {"Organic Search": int, ...}, "previous": {...}},
            "leads": {"current": int, "previous": int} | None,
        }
        """
        period_b_start, period_b_end = previous_year_range(start_date, end_date)
        return self._yoy_summary(
            credentials, property_id, start_date, end_date, period_b_start, period_b_end, lead_event_name
        )

    def fetch_3m_yoy_summary(
        self, credentials: Credentials, property_id: str, lead_event_name: str | None = None
    ) -> dict:
        """Zgodność wsteczna: porównanie R/R dla ostatnich {YOY_COMPARISON_MONTHS}
        pełnych miesięcy kalendarzowych. Używane przy pierwszym podłączeniu usługi GA4
        (`AuditService._refresh_ga4_insights`), gdy użytkownik nie wskazał jeszcze
        własnego zakresu."""
        period_a_start, period_a_end = last_n_full_months_range(YOY_COMPARISON_MONTHS)
        period_b_start, period_b_end = same_months_last_year(period_a_start, period_a_end)
        return self._yoy_summary(
            credentials, property_id, period_a_start, period_a_end,
            period_b_start, period_b_end, lead_event_name,
        )

    def _yoy_summary(
        self,
        credentials: Credentials,
        property_id: str,
        period_a_start: date,
        period_a_end: date,
        period_b_start: date,
        period_b_end: date,
        lead_event_name: str | None,
    ) -> dict:
        """Wspólne ciało obu wariantów porównania R/R (dowolny zakres i 3 pełne miesiące)."""

        channels_current = self.fetch_channel_totals(credentials, property_id, period_a_start, period_a_end)
        channels_previous = self.fetch_channel_totals(credentials, property_id, period_b_start, period_b_end)

        leads = None
        if lead_event_name:
            leads = {
                "current": self.fetch_event_total(
                    credentials, property_id, lead_event_name, period_a_start, period_a_end
                ),
                "previous": self.fetch_event_total(
                    credentials, property_id, lead_event_name, period_b_start, period_b_end
                ),
            }

        return {"channels": {"current": channels_current, "previous": channels_previous}, "leads": leads}

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
        return {"total_sessions": 0, "granularity": "day", "history": {"dates": [], "sessions": []}}

    def _empty_channel_history(self) -> dict:
        return {"months": [], "channels": {}}

    def _empty_event_history(self) -> dict:
        return {"total_events": 0, "history": {"months": [], "events": []}}
