from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from django.db import transaction

from .ga4_insights import analyze_channel_trends
from .ga4_service import GA4OAuthService
from .gsc_insights import generate_page_commentary, generate_query_commentary
from .gsc_service import GSCService
from .pagespeed import PageSpeedService
from .rag import RAGEngine
from .scraper import (
    AI_BOT_USER_AGENTS,
    ANSWER_FIRST_MAX_WORDS,
    ANSWER_FIRST_MIN_WORDS,
    ScraperError,
    SEOScraper,
)
from .senuto import SenutoService
from .url_guard import UnsafeUrlError, validate_public_url
from .wayback import WaybackService

if TYPE_CHECKING:
    from google.oauth2.credentials import Credentials

    from auditor.models import Audit

logger = logging.getLogger(__name__)

TITLE_MIN_LENGTH = 30
TITLE_MAX_LENGTH = 65
DESCRIPTION_MIN_LENGTH = 70
DESCRIPTION_MAX_LENGTH = 160

# Progi Google Core Web Vitals (granica "dobry" / granica "wymaga poprawy")
PAGESPEED_SCORE_GOOD = 90
PAGESPEED_SCORE_WARNING = 50
LCP_GOOD = 2.5
LCP_WARNING = 4.0
CLS_GOOD = 0.1
CLS_WARNING = 0.25
FCP_GOOD = 1.8
FCP_WARNING = 3.0
INP_GOOD = 200
INP_WARNING = 500

# Oczekiwane typy Schema.org w zależności od wykrytego typu podstrony (auditor.services.scraper).
# Każda "grupa" to zbiór alternatyw - wystarczy, że wystąpi jeden z typów w grupie.
PAGE_TYPE_LABELS = {
    "homepage": "Strona główna",
    "product": "Strona produktowa",
    "article": "Artykuł / Blog",
    "category": "Kategoria / Sklep",
    "generic": "Strona ogólna",
}

EXPECTED_SCHEMA_BY_PAGE_TYPE = {
    "homepage": [{"Organization", "LocalBusiness"}, {"WebSite"}],
    "product": [{"Product"}],
    "article": [{"Article", "BlogPosting"}],
    "category": [{"CollectionPage", "ItemList"}],
    "generic": [{"WebPage"}],
}

# Testy EEAT+ (autorstwo, data aktualizacji) opisują wymóg dotyczący przede
# wszystkim treści blogowych/poradnikowych (YMYL) - na stronach głównych, klastrach
# szkół/placówek i stronach usługowych/ofertowych ich brak jest OPCJONALNY
# (status INFO), a nie realnym problemem do naprawy (patrz _evaluate_eeat_*).
EEAT_REQUIRED_PAGE_TYPES = {"article"}

# Minimalna liczba linków wewnętrznych, poniżej której zgłaszamy ostrzeżenie.
INTERNAL_LINKING_MIN = 3

# Progi wieku domeny wg archiwum Internet Archive (patrz _evaluate_wayback_domain_age).
DOMAIN_AGE_ESTABLISHED_YEARS = 2.0
DOMAIN_AGE_YOUNG_YEARS = 0.5

# Progi testu "thin content" (liczba słów widocznej treści).
THIN_CONTENT_MIN_WORDS = 300
THIN_CONTENT_CRITICAL_WORDS = 100

# Typy Schema.org, po których modele językowe budują odpowiedzi o firmie i jej ofercie -
# ich obecność zwiększa szansę na cytowanie witryny w wynikach generowanych przez AI.
AI_RELEVANT_SCHEMA_TYPES = ("Organization", "SoftwareApplication", "FAQPage", "Product")

# Progi wzorca Answer-First: jaki udział sekcji musi zaczynać się zwięzłą odpowiedzią,
# żeby treść uznać za przygotowaną pod cytowanie w odpowiedziach AI.
# Tolerancja różnicy cen (zaokrąglenia, prezentacja brutto/netto) przed zgłoszeniem błędu.
PRICE_DISCREPANCY_TOLERANCE = 0.02

# Od tylu brakujących właściwości e-commerce zgłaszamy błąd zamiast ostrzeżenia.
ECOMMERCE_CRITICAL_MISSING = 3

# Minimalna liczba słów, poniżej której strona kolekcji jest praktycznie pusta.
COLLECTION_MIN_WORDS = 50

ANSWER_FIRST_GOOD_RATIO = 0.5
ANSWER_FIRST_WEAK_RATIO = 0.2

# Udział tabel i list wśród bloków treści, powyżej którego strukturę uznajemy za dobrą.
STRUCTURED_CONTENT_GOOD_SHARE = 0.2

# --- MODUŁ: głęboka walidacja grafu Schema.org ------------------------------
# Właściwości, przez które encje powinny wskazywać na siebie referencją @id zamiast
# powielać pełne definicje (klucz: typ encji, wartość: (właściwość, oczekiwany typ celu)).
#
# Lista obejmuje WYŁĄCZNIE encje współdzielone między podstronami - firmę, witrynę
# i autora opisuje się raz, a kolejne strony powinny je referencjonować. Encje należące
# do jednego obiektu (Product.offers, Product.brand, Offer.priceSpecification) są
# zagnieżdżane z definicji i wymaganie dla nich @id dawałoby fałszywe alarmy na
# poprawnie zbudowanych kartach produktu.
SCHEMA_EXPECTED_LINKS = {
    "WebPage": [("isPartOf", "WebSite"), ("publisher", "Organization")],
    "WebSite": [("publisher", "Organization")],
    "Offer": [("seller", "Organization")],
    "Article": [("publisher", "Organization"), ("author", "Person")],
    "BlogPosting": [("publisher", "Organization"), ("author", "Person")],
}

# Typy, których zduplikowanie (kilka encji tego samego typu z różnymi @id) oznacza
# rozjechany graf - wyszukiwarka nie wie wtedy, która encja opisuje firmę.
SCHEMA_SINGLETON_TYPES = ("Organization", "WebSite", "WebPage")

# Ciągi zdradzające adres środowiska deweloperskiego, który wyciekł do produkcji.
SCHEMA_ENV_LEAK_MARKERS = (".test", "localhost", ".local", "127.0.0.1", ".dev.", "staging.")

# Ślady podwójnego kodowania encji HTML - tekst przepuszczony dwa razy przez escaping.
SCHEMA_DOUBLE_ENCODING_MARKERS = ("&amp;amp;", "&amp;quot;", "&amp;#", "&amp;nbsp;", "&amp;lt;", "&amp;gt;")

# Separatory, po których w nazwach produktów doklejana bywa nazwa domeny.
SCHEMA_NAME_SEPARATORS = (" | ", " - ", " – ", " — ")

# Właściwości wymagane na karcie produktu, żeby trafiła do AI Overviews i rich results.
PRODUCT_REQUIRED_PROPERTIES = ("brand", "aggregateRating_or_review", "shippingDetails", "returnPolicy")

# --- MODUŁ: E-E-A-T -------------------------------------------------------
# Właściwości encji Person dowodzące kompetencji autora (samo imię i nazwisko nie
# wystarcza, by wykazać "Experience" i "Expertise" z wytycznych Google).
AUTHOR_EXPERTISE_PROPERTIES = ("jobTitle", "description", "knowsAbout", "hasCredential")

# Po ilu dniach od publikacji brak aktualizacji treści uznajemy za sygnał przestarzałości.
FRESHNESS_DECAY_DAYS = 365

# Minimalna liczba linków do źródeł zewnętrznych w treści poradnikowej.
MIN_TRUSTED_SOURCES = 1

# --- MODUŁ: GEO Suppression -----------------------------------------------
# Udział ukrytej treści, powyżej którego zgłaszamy błąd krytyczny dostępności dla AI.
HIDDEN_CONTENT_CRITICAL_SHARE = 0.3
HIDDEN_CONTENT_WARNING_SHARE = 0.1

# Teksty zastępcze, które nie powinny trafić na produkcję.
PLACEHOLDER_MARKERS = (
    "lorem ipsum", "dolor sit amet", "tekst zastępczy", "tekst do uzupełnienia",
    "opis w przygotowaniu", "wpisz opis", "todo:", "tbd", "placeholder",
    "brak opisu", "przykładowy tekst",
)

# Progi udziału elementów ustrukturyzowanych zależne od typu podstrony - karta produktu
# powinna mieć więcej danych tabelarycznych niż artykuł (specyfikacja, parametry).
STRUCTURED_SHARE_BY_PAGE_TYPE = {"product": 0.15, "article": 0.10}

SCORE_WEIGHTS = {"ok": 100, "info": 100, "warning": 50, "error": 0}

# Ile szablonów podstron skanujemy równolegle. Każdy wątek to scraping + 2 zapytania do
# PageSpeed, więc wyższa wartość nie przyspieszy audytu (limity API Google), a zwiększy
# ryzyko odrzucenia żądań po stronie audytowanego serwera.
MAX_PARALLEL_PAGE_SCANS = 3


class _NullRecommendationEngine:
    """Zamiennik `RAGEngine` dla skanu podstron - nie generuje rekomendacji AI.

    Skanowanie 4-5 szablonów z pełnym RAG oznaczałoby kilkukrotnie większy koszt i czas
    audytu przy niemal identycznych poradach (problemy szablonowe powtarzają się na
    całej witrynie). Rekomendacje powstają raz, dla adresu głównego.
    """

    def generate_recommendation(self, *args, **kwargs) -> str:
        return ""


class AuditService:
    """Orkiestrator audytu SEO: SEOScraper + PageSpeedService -> analiza metryk -> RAGEngine -> zapis do bazy."""

    def __init__(
        self,
        scraper: SEOScraper | None = None,
        rag_engine: RAGEngine | None = None,
        pagespeed_service: PageSpeedService | None = None,
        senuto_service: SenutoService | None = None,
        ga4_service: GA4OAuthService | None = None,
        gsc_service: GSCService | None = None,
        wayback_service: WaybackService | None = None,
    ):
        self.scraper = scraper or SEOScraper()
        self.rag_engine = rag_engine or RAGEngine()
        self.pagespeed_service = pagespeed_service or PageSpeedService()
        self.senuto_service = senuto_service or SenutoService()
        self.ga4_service = ga4_service or GA4OAuthService()
        self.gsc_service = gsc_service or GSCService()
        self.wayback_service = wayback_service or WaybackService()

    def run_audit(self, audit: "Audit") -> "Audit":
        from auditor.models import Audit, AuditMetric

        audit.status = Audit.Status.PROCESSING
        audit.save(update_fields=["status"])

        try:
            try:
                data = self.scraper.scrape(audit.url)
            except ScraperError:
                logger.warning("Audyt %s nie powiódł się.", audit.pk, exc_info=True)
                return audit

            metrics = self._build_metrics(data)
            metrics.extend(self._build_pagespeed_metrics(audit.url))
            metrics.extend(self._build_extra_checks_metrics(audit.url, data))

            # Jedna transakcja + bulk_create zamiast ~30 osobnych INSERT-ów: bez tego
            # wyjątek w połowie pętli zostawiał audyt z niekompletnym zestawem metryk.
            with transaction.atomic():
                audit.metrics.all().delete()
                AuditMetric.objects.bulk_create(
                    [AuditMetric(audit=audit, **metric) for metric in metrics]
                )

            # Adres główny trafia też do zestawienia szablonów - bez ponownego skanowania,
            # bo jego metryki są już policzone powyżej.
            self._store_primary_page(audit, metrics)
            self._scan_additional_pages(audit)

            senuto_stats = self.senuto_service.get_visibility_stats(audit.url)
            audit.senuto_top3 = senuto_stats["top3"]
            audit.senuto_top10 = senuto_stats["top10"]
            audit.senuto_top50 = senuto_stats["top50"]
            audit.senuto_history = senuto_stats["history"]

            audit.score = self._calculate_score(metrics)
            audit.status = Audit.Status.COMPLETED
            audit.save(
                update_fields=[
                    "score",
                    "status",
                    "senuto_top3",
                    "senuto_top10",
                    "senuto_top50",
                    "senuto_history",
                ]
            )
            return audit
        finally:
            # Każde wyjście z metody inne niż ukończony audyt musi zamknąć rekord
            # statusem FAILED - inaczej audyt zostaje w PROCESSING na zawsze, a
            # interfejs w nieskończoność pokazuje "Audyt jest jeszcze przetwarzany".
            if audit.status == Audit.Status.PROCESSING:
                audit.status = Audit.Status.FAILED
                audit.save(update_fields=["status"])

    # ------------------------------------------------------------------
    # Audyt wielu szablonów podstron (auditor.models.AuditedPage)
    # ------------------------------------------------------------------
    def _store_primary_page(self, audit: "Audit", metrics: list[dict]) -> None:
        """Zapisuje metryki adresu głównego jako `AuditedPage` typu "homepage".

        Bez ponownego skanowania - te same metryki, które właśnie trafiły do
        `AuditMetric`, lądują w zestawieniu szablonów, żeby matryca eksportu obejmowała
        całą witrynę, a nie tylko podstrony dodatkowe.
        """
        from auditor.models import AuditedPage

        AuditedPage.objects.update_or_create(
            audit=audit,
            url=audit.url,
            defaults={
                "page_type": AuditedPage.PageType.HOMEPAGE,
                "metrics_data": metrics,
                "status": AuditedPage.Status.COMPLETED,
                "score": self._calculate_score(metrics),
                "error_message": "",
            },
        )

    def _scan_additional_pages(self, audit: "Audit") -> None:
        """Skanuje wszystkie dodatkowe szablony podstron zadeklarowane przy audycie.

        Podział pracy: wątki robocze wykonują WYŁĄCZNIE operacje sieciowe i obliczenia
        (scraping, PageSpeed, ocena metryk), a wszystkie zapisy do bazy wykonuje wątek
        główny po zebraniu wyników. Zapisywanie z wątków roboczych blokowało SQLite
        ("database table is locked") i wymagałoby ręcznego zarządzania połączeniami.

        Błąd pojedynczej podstrony nie przerywa audytu ani nie wpływa na pozostałe -
        rekord dostaje status FAILED i komunikat.
        """
        from auditor.models import AuditedPage

        pages = list(audit.pages.exclude(url=audit.url))
        if not pages:
            return

        logger.info("Audyt %s: skanowanie %s dodatkowych szablonów podstron.", audit.pk, len(pages))

        results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=min(len(pages), MAX_PARALLEL_PAGE_SCANS)) as executor:
            futures = {executor.submit(self._compute_page_metrics, page.url): page for page in pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    results[page.pk] = future.result()
                except Exception:
                    logger.exception("Nie udało się przeskanować podstrony %s (audyt %s).", page.url, audit.pk)
                    results[page.pk] = {
                        "metrics": [],
                        "error": "Nieoczekiwany błąd podczas skanowania podstrony.",
                    }

        for page in pages:
            result = results.get(page.pk, {"metrics": [], "error": "Brak wyniku skanowania."})
            if result["error"]:
                page.status = AuditedPage.Status.FAILED
                page.error_message = result["error"]
                page.metrics_data = []
                page.score = 0
            else:
                page.status = AuditedPage.Status.COMPLETED
                page.error_message = ""
                page.metrics_data = result["metrics"]
                page.score = self._calculate_score(result["metrics"])
            page.save(update_fields=["status", "error_message", "metrics_data", "score"])

    def _compute_page_metrics(self, url: str) -> dict:
        """Liczy metryki jednej podstrony. NIE dotyka bazy danych - patrz `_scan_additional_pages`.

        Rekomendacje AI są tu CELOWO pomijane (`_NullRecommendationEngine`): generowanie
        ich osobno dla każdego szablonu oznaczałoby kilkukrotnie większy koszt i czas
        OpenAI, a problemy szablonowe najczęściej powtarzają się na całej witrynie.
        Eksport uzupełnia kolumnę "Rekomendacja AI" rekomendacją z audytu głównego dla
        tego samego klucza metryki (patrz `auditor.services.exporter`).

        Zwraca {"metrics": [...], "error": str} - błąd zamiast wyjątku, żeby wątek
        roboczy nie przerywał skanowania pozostałych szablonów.
        """
        # Wstrzyknięte zależności przekazujemy dalej (mockowalność w testach), podmieniając
        # wyłącznie silnik rekomendacji.
        service = AuditService(
            scraper=self.scraper,
            rag_engine=_NullRecommendationEngine(),
            pagespeed_service=self.pagespeed_service,
            senuto_service=self.senuto_service,
            ga4_service=self.ga4_service,
            gsc_service=self.gsc_service,
            wayback_service=self.wayback_service,
        )

        try:
            safe_url = validate_public_url(url)
        except UnsafeUrlError as exc:
            return {"metrics": [], "error": str(exc)}

        try:
            data = service.scraper.scrape(safe_url)
        except ScraperError as exc:
            return {"metrics": [], "error": f"Nie udało się pobrać podstrony: {exc}"}

        metrics = service._build_metrics(data)
        metrics.extend(service._build_pagespeed_metrics(safe_url))
        metrics.extend(service._build_extra_checks_metrics(safe_url, data))
        return {"metrics": metrics, "error": ""}

    # ------------------------------------------------------------------
    # Google Analytics 4 (OAuth 2.0) -> ruch organiczny
    # ------------------------------------------------------------------
    def sync_ga4_data(self, audit: "Audit", credentials: "Credentials", property_id: str, days: int = 30) -> "Audit":
        """Pobiera z GA4 dzienną historię sesji z ruchu organicznego dla `property_id`
        i zapisuje wyniki na `audit` przez Django ORM (`auditor.services.ga4_service.GA4OAuthService`).

        Wywoływana z `auditor.views.ga4_callback` po zakończeniu przepływu OAuth 2.0 -
        NIE jest częścią `run_audit()`, ponieważ wymaga wcześniej uzyskanych `credentials`
        (użytkownik musi najpierw przejść przez ekran zgody Google). Błąd komunikacji
        z GA4 nie usuwa już zapisanego `ga4_refresh_token` - użytkownik może spróbować
        odświeżyć dane później bez ponownego logowania się przez Google.
        """
        try:
            stats = self.ga4_service.fetch_organic_traffic(credentials, property_id, days=days)
        except Exception:
            logger.exception("Nie udało się pobrać danych GA4 dla audytu %s (property_id=%s).", audit.pk, property_id)
            return audit

        audit.ga4_property_id = property_id
        audit.ga4_organic_sessions = stats["total_sessions"]
        audit.ga4_history = stats["history"]

        # Dane wielokanałowe (12 mies.) i automatyczne wnioski SEO liczymy od razu przy
        # podłączeniu usługi, żeby sekcja analizy była widoczna zanim użytkownik wybierze
        # zdarzenie lead/konwersja (patrz `refresh_ga4_lead_event`).
        self._refresh_ga4_insights(audit, credentials, property_id)

        audit.save(
            update_fields=[
                "ga4_property_id",
                "ga4_organic_sessions",
                "ga4_history",
                "ga4_channels_history",
                "ga4_insights",
            ]
        )

        # GSC używa tych samych `credentials` (scope webmasters.readonly jest proszony
        # łącznie z analytics.readonly, patrz settings.GA4_SCOPES) - błąd/brak dostępu
        # nie przerywa audytu, pola GSC zostają wtedy przy wartościach domyślnych.
        self.sync_gsc_data(audit, credentials)
        return audit

    # ------------------------------------------------------------------
    # Google Search Console (OAuth 2.0) -> analiza fraz kluczowych 3M YoY
    # ------------------------------------------------------------------
    def sync_gsc_data(self, audit: "Audit", credentials: "Credentials") -> "Audit":
        """Pobiera i zapisuje porównanie 3M R/R (ostatnie 3 pełne miesiące vs
        analogiczne 3 miesiące rok temu) zarówno dla fraz kluczowych, jak i
        podstron, wraz z automatycznymi komentarzami tekstowymi
        (`auditor.services.gsc_insights`). Dopasowanie usługi Search Console do
        `audit.url` (obsługujące sc-domain:, http/https, z/bez "www.") leży po
        stronie `GSCService` (patrz `gsc_service.find_best_gsc_site`). Brak dostępu
        do Search Console (domena niezarejestrowana, refresh_token sprzed dodania
        tego scope'u, błąd API) nie jest traktowany jak błąd audytu - pola GSC
        zostają przy wartościach domyślnych (0 / puste listy / pusty tekst)."""
        query_stats = self.gsc_service.fetch_yoy_query_performance(credentials, audit.url)
        page_stats = self.gsc_service.fetch_yoy_page_performance(credentials, audit.url)

        audit.gsc_total_clicks_current = query_stats["total_clicks_current"]
        audit.gsc_total_clicks_previous = query_stats["total_clicks_previous"]
        audit.gsc_yoy_change_percent = query_stats["yoy_change_percent"]
        audit.gsc_top_gainers = query_stats["top_gainers"]
        audit.gsc_top_losers = query_stats["top_losers"]
        audit.gsc_top_page_gainers = page_stats["top_gainers"]
        audit.gsc_top_page_losers = page_stats["top_losers"]
        audit.gsc_query_commentary = generate_query_commentary(query_stats)
        audit.gsc_page_commentary = generate_page_commentary(page_stats)
        audit.save(
            update_fields=[
                "gsc_total_clicks_current",
                "gsc_total_clicks_previous",
                "gsc_yoy_change_percent",
                "gsc_top_gainers",
                "gsc_top_losers",
                "gsc_top_page_gainers",
                "gsc_top_page_losers",
                "gsc_query_commentary",
                "gsc_page_commentary",
            ]
        )
        return audit

    def refresh_ga4_lead_event(
        self, audit: "Audit", credentials: "Credentials", event_name: str | None
    ) -> "Audit":
        """Zapisuje wybrane przez użytkownika zdarzenie lead/konwersja i przelicza
        wnioski SEO ponownie (w tym trend tego zdarzenia z ruchu organicznego).

        Wywoływana z formularza wyboru zdarzenia w `auditor.views.audit_detail`.
        `event_name=None` czyści wybór (wnioski są wtedy liczone bez trendu leadów).
        """
        audit.ga4_selected_lead_event = event_name
        self._refresh_ga4_insights(audit, credentials, audit.ga4_property_id, event_name=event_name)
        audit.save(update_fields=["ga4_selected_lead_event", "ga4_channels_history", "ga4_insights"])
        return audit

    def _refresh_ga4_insights(
        self, audit: "Audit", credentials: "Credentials", property_id: str, event_name: str | None = "__unset__"
    ) -> None:
        """Pobiera 12-miesięczne dane wielokanałowe GA4 (do wykresu), zagregowane
        sumy 3M R/R per kanał (do "Automatycznych Wniosków SEO") oraz - jeśli
        wybrano - miesięczną i 3M R/R historię zdarzenia lead/konwersja z ruchu
        organicznego, po czym wylicza `ga4_insights` przez
        `auditor.services.ga4_insights.analyze_channel_trends`. Ustawia pola na `audit`
        w pamięci - zapis do bazy (`audit.save()`) leży po stronie wywołującego."""
        if event_name == "__unset__":
            event_name = audit.ga4_selected_lead_event

        channels_data = self.ga4_service.fetch_yearly_channel_data(credentials, property_id)
        audit.ga4_channels_history = channels_data

        lead_history = None
        if event_name:
            conversions = self.ga4_service.fetch_event_conversions(credentials, property_id, event_name)
            lead_history = conversions["history"]

        summary_3m = self.ga4_service.fetch_3m_yoy_summary(credentials, property_id, lead_event_name=event_name)
        audit.ga4_insights = analyze_channel_trends(
            summary_3m["channels"], lead_history=lead_history, lead_totals_3m=summary_3m["leads"]
        )

    # ------------------------------------------------------------------
    # Analiza danych ze scrapera -> metryki
    # ------------------------------------------------------------------
    def _build_metrics(self, data: dict) -> list[dict]:
        return [
            self._evaluate_title(data),
            self._evaluate_description(data),
            self._evaluate_h1(data),
            self._evaluate_canonical(data),
            self._evaluate_open_graph(data),
            self._evaluate_images(data),
            self._evaluate_schema_page_type(data),
            self._evaluate_schema_breadcrumbs(data),
            self._evaluate_schema_faq(data),
            self._evaluate_schema_validity(data),
            self._evaluate_schema_entity_linking(data),
            self._evaluate_price_discrepancy(data),
            self._evaluate_schema_data_hygiene(data),
            self._evaluate_ecommerce_completeness(data),
            self._evaluate_authorship_depth(data),
            self._evaluate_freshness_decay(data),
            self._evaluate_external_sources(data),
            self._evaluate_hidden_content(data),
            self._evaluate_heading_visibility(data),
            self._evaluate_schema_html_parity(data),
            self._evaluate_placeholder_content(data),
            self._evaluate_answer_first(data),
            self._evaluate_structured_content(data),
            self._evaluate_twitter_cards(data),
            self._evaluate_favicon(data),
            self._evaluate_thin_content(data),
            self._evaluate_heading_order(data),
            self._evaluate_heading_noise(data),
            self._evaluate_image_quality(data),
            self._evaluate_eeat_authorship(data),
            self._evaluate_eeat_freshness(data),
            self._evaluate_meta_keywords(data),
            self._evaluate_internal_linking(data),
            self._evaluate_js_rendering(data),
            self._evaluate_redirects(data),
        ]

    def _evaluate_answer_first(self, data: dict) -> dict:
        """GEO: czy sekcje treści zaczynają się od zwięzłej, bezpośredniej odpowiedzi.

        Modele językowe cytują fragmenty, które odpowiadają na pytanie od razu. Sekcja
        rozpoczynająca się od długiego wstępu rzadko trafia do odpowiedzi AI w całości -
        model musi ją wtedy streścić sam, a wtedy równie dobrze może sięgnąć po źródło
        konkurencji. Wzorzec "Answer-First" to akapit
        {ANSWER_FIRST_MIN_WORDS}-{ANSWER_FIRST_MAX_WORDS} słów zaraz pod nagłówkiem.

        Strona bez nagłówków sekcji (np. landing page) dostaje status INFO, a nie
        ostrzeżenie - wzorzec dotyczy treści dzielonej na sekcje, a nie każdej strony.
        """
        answer_first = data.get("answer_first", {})
        total = answer_first.get("sections_total", 0)
        compliant = answer_first.get("sections_compliant", 0)
        ratio = answer_first.get("ratio", 0.0)

        if not total:
            return self._make_metric(
                "structure",
                "answer_first",
                {
                    "sections_total": 0,
                    "sections_compliant": 0,
                    "ratio": 0.0,
                    "note": "Strona nie ma nagłówków sekcji H2/H3 - wzorzec Answer-First nie ma tu zastosowania.",
                },
                "info",
                current_value="(brak nagłówków sekcji H2/H3)",
                generate_recommendation=False,
            )

        if ratio >= ANSWER_FIRST_GOOD_RATIO:
            status = "ok"
            note = (
                f"{compliant} z {total} sekcji zaczyna się od zwięzłej odpowiedzi "
                f"({ANSWER_FIRST_MIN_WORDS}-{ANSWER_FIRST_MAX_WORDS} słów) - treść jest gotowa do cytowania przez AI."
            )
        elif ratio >= ANSWER_FIRST_WEAK_RATIO:
            status = "warning"
            note = (
                f"Tylko {compliant} z {total} sekcji zaczyna się od zwięzłej odpowiedzi. Dodanie "
                f"akapitu {ANSWER_FIRST_MIN_WORDS}-{ANSWER_FIRST_MAX_WORDS} słów pod nagłówkami "
                "zwiększa szansę na cytowanie w odpowiedziach AI."
            )
        else:
            status = "warning"
            note = (
                f"Żadna lub prawie żadna sekcja ({compliant}/{total}) nie zaczyna się od bezpośredniej "
                "odpowiedzi - modele językowe muszą streszczać treść samodzielnie, co obniża szansę "
                "na zacytowanie tej strony."
            )

        przyklady = [
            f"{s['heading']}: {s['word_count']} słów"
            for s in answer_first.get("sections", [])[:5]
        ]
        return self._make_metric(
            "structure",
            "answer_first",
            {
                "sections_total": total,
                "sections_compliant": compliant,
                "ratio": ratio,
                "sections": answer_first.get("sections", []),
                "note": note,
            },
            status,
            current_value="; ".join(przyklady) or "(brak akapitów pod nagłówkami sekcji)",
        )

    def _evaluate_structured_content(self, data: dict) -> dict:
        """GEO: udział natywnych tabel i list w treści strony.

        `<table>`, `<ul>` i `<ol>` niosą jawną strukturę, którą model odczytuje wprost -
        z prozy musi ją dopiero wywnioskować. Strony z zestawieniami w tabelach i listach
        są chętniej cytowane w odpowiedziach generatywnych, zwłaszcza przy pytaniach
        porównawczych ("czym różni się X od Y", "ile kosztuje").
        """
        structured = data.get("structured_content", {})
        tables = structured.get("tables", 0)
        lists = structured.get("lists", 0) + structured.get("definition_lists", 0)
        # Karta produktu powinna mieć więcej danych tabelarycznych niż artykuł - to tam
        # mieszka specyfikacja, z której model wyciąga parametry i porównania.
        prog_typu = STRUCTURED_SHARE_BY_PAGE_TYPE.get(
            data.get("page_type", "generic"), STRUCTURED_CONTENT_GOOD_SHARE
        )
        share = structured.get("share", 0.0)
        blocks = structured.get("structured_blocks", 0)
        paragraphs = structured.get("paragraphs", 0)

        if not blocks and not paragraphs:
            return self._make_metric(
                "structure",
                "structured_content",
                {
                    "tables": 0, "lists": 0, "share": 0.0,
                    "note": "Strona nie zawiera treści tekstowej, w której można zmierzyć udział tabel i list.",
                },
                "info",
                current_value="(brak treści do analizy)",
                generate_recommendation=False,
            )

        if not blocks:
            status = "warning"
            note = (
                "Treść składa się wyłącznie z akapitów - brak tabel i list. Zestawienia, kroki "
                "i porównania podane w formie listy lub tabeli są znacznie chętniej cytowane przez AI."
            )
        elif share >= prog_typu:
            status = "ok"
            note = (
                f"Treść jest dobrze ustrukturyzowana: {tables} tabel i {lists} list "
                f"({int(share * 100)}% bloków treści) - format czytelny dla modeli językowych."
            )
        else:
            status = "warning"
            note = (
                f"Niski udział elementów ustrukturyzowanych: {tables} tabel i {lists} list na "
                f"{paragraphs} akapitów ({int(share * 100)}% bloków treści). Rozważ zamianę części "
                "wyliczeń w prozie na listy, a danych porównawczych na tabele."
            )

        return self._make_metric(
            "structure",
            "structured_content",
            {
                "tables": tables,
                "lists": lists,
                "definition_lists": structured.get("definition_lists", 0),
                "definition_pairs": structured.get("definition_pairs", 0),
                "threshold": prog_typu,
                "list_items": structured.get("list_items", 0),
                "paragraphs": paragraphs,
                "share": share,
                "note": note,
            },
            status,
            current_value=(
                f"Tabele: {tables}, listy: {lists} ({structured.get('list_items', 0)} pozycji), "
                f"akapity: {paragraphs}"
            ),
        )

    # ------------------------------------------------------------------
    # MODUŁ: głęboka walidacja grafu Schema.org i jakości danych
    # ------------------------------------------------------------------
    def _schema_entities(self, data: dict) -> list[dict]:
        return [e for e in (data.get("schema", {}).get("entities") or []) if isinstance(e, dict)]

    def _entity_types(self, entity: dict) -> list[str]:
        """Typ encji jako lista - JSON-LD dopuszcza zarówno string, jak i tablicę typów."""
        raw = entity.get("@type")
        if isinstance(raw, list):
            return [str(t) for t in raw]
        return [str(raw)] if raw else []

    def _entities_of_type(self, entities: list[dict], type_name: str) -> list[dict]:
        return [e for e in entities if type_name in self._entity_types(e)]

    def _is_reference(self, value) -> bool:
        """Czy wartość właściwości jest referencją @id, a nie zagnieżdżoną encją."""
        if isinstance(value, str):
            return True
        if isinstance(value, dict):
            # {"@id": "..."} bez @type to czysta referencja; z @type to duplikat definicji.
            return "@id" in value and not value.get("@type")
        if isinstance(value, list):
            return any(self._is_reference(item) for item in value)
        return False

    def _evaluate_schema_entity_linking(self, data: dict) -> dict:
        """Powiązania encji w grafie: referencje @id zamiast duplikowanych definicji.

        Modele językowe budują odpowiedź z RELACJI między encjami - "ten produkt jest
        sprzedawany przez tę firmę, opisaną na tej stronie". Graf, w którym każda encja
        powtarza pełną definicję firmy zamiast wskazywać na nią przez @id, jest dla
        modelu zbiorem luźnych obiektów, a nie spójnym opisem biznesu.
        """
        entities = self._schema_entities(data)
        if not entities:
            return self._make_metric(
                "structure", "schema_entity_linking",
                {"entities": 0, "missing_links": [], "duplicates": [], "note":
                 "Brak danych strukturalnych JSON-LD - nie ma grafu encji do zweryfikowania."},
                "error",
                current_value="(brak encji JSON-LD)",
            )

        with_id = [e for e in entities if e.get("@id")]
        duplicates: list[str] = []
        for type_name in SCHEMA_SINGLETON_TYPES:
            same_type = self._entities_of_type(entities, type_name)
            unique_ids = {e.get("@id") for e in same_type if e.get("@id")}
            # Dwie encje tego samego typu z RÓŻNYMI @id to rozjechany graf.
            if len(same_type) > 1 and len(unique_ids) > 1:
                duplicates.append(f"{type_name} ({len(unique_ids)} różnych @id)")

        missing_links: list[str] = []
        for entity in entities:
            for type_name in self._entity_types(entity):
                for prop, target in SCHEMA_EXPECTED_LINKS.get(type_name, []):
                    if prop not in entity:
                        continue
                    if not self._is_reference(entity[prop]):
                        missing_links.append(f"{type_name}.{prop} (zagnieżdżona encja zamiast @id -> {target})")

        if not with_id:
            status = "warning"
            note = (
                f"Żadna z {len(entities)} encji JSON-LD nie ma identyfikatora @id - encje nie mogą "
                "się wzajemnie referencjonować, a graf pozostaje zbiorem luźnych obiektów."
            )
        elif duplicates:
            status = "error"
            note = (
                "Graf zawiera zduplikowane encje zamiast referencji: " + ", ".join(duplicates) +
                ". Wyszukiwarka nie wie, która encja opisuje firmę."
            )
        elif missing_links:
            status = "warning"
            note = (
                f"Wykryto {len(missing_links)} powiązań zapisanych jako zagnieżdżone encje zamiast "
                "referencji @id: " + "; ".join(missing_links[:3]) + "."
            )
        else:
            status = "ok"
            note = f"Graf jest spójny: {len(with_id)} z {len(entities)} encji ma @id, powiązania używają referencji."

        return self._make_metric(
            "structure", "schema_entity_linking",
            {
                "entities": len(entities),
                "entities_with_id": len(with_id),
                "missing_links": missing_links,
                "duplicates": duplicates,
                "note": note,
            },
            status,
            current_value="; ".join(duplicates + missing_links[:5]) or f"Encje z @id: {len(with_id)}/{len(entities)}",
        )

    def _collect_schema_prices(self, entities: list[dict]) -> list[float]:
        """Ceny zadeklarowane w grafie (Offer.price, UnitPriceSpecification.price, lowPrice)."""
        prices: list[float] = []
        for entity in entities:
            for prop in ("price", "lowPrice", "highPrice"):
                raw = entity.get(prop)
                if raw is None:
                    continue
                try:
                    value = float(str(raw).replace(",", ".").replace(" ", ""))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    prices.append(value)
        return prices

    def _evaluate_price_discrepancy(self, data: dict) -> dict:
        """Zgodność ceny z danych strukturalnych z ceną widoczną na stronie.

        Błąd krytyczny w e-commerce: gdy do Schema trafia cena hurtowa/B2B pobrana wprost
        z bazy, a użytkownik widzi na ekranie cenę detaliczną, wyszukiwarka i modele AI
        obiecują cenę, której na stronie nie ma. Poza utratą zaufania grozi to karą za
        niezgodne dane strukturalne.
        """
        entities = self._schema_entities(data)
        schema_prices = self._collect_schema_prices(entities)
        visible = data.get("visible_prices", {})
        min_visible = visible.get("min_visible")

        if not schema_prices:
            return self._make_metric(
                "structure", "price_discrepancy",
                {"schema_prices": [], "visible_min": min_visible, "note":
                 "Strona nie deklaruje ceny w danych strukturalnych - test nie ma zastosowania."},
                "info",
                current_value="(brak ceny w JSON-LD)",
                generate_recommendation=False,
            )

        if min_visible is None:
            return self._make_metric(
                "structure", "price_discrepancy",
                {"schema_prices": schema_prices, "visible_min": None, "note":
                 f"Schema deklaruje cenę ({min(schema_prices)}), ale na stronie nie wykryto żadnej "
                 "widocznej ceny - zweryfikuj, czy cena nie jest dorysowywana dopiero przez JavaScript."},
                "warning",
                current_value=f"Cena w Schema: {min(schema_prices)}; widoczna cena: brak",
            )

        schema_min = min(schema_prices)
        # Tolerancja na zaokrąglenia i drobne różnice brutto/netto w prezentacji.
        prog = min_visible * (1 - PRICE_DISCREPANCY_TOLERANCE)

        if schema_min < prog:
            status = "error"
            roznica = round((min_visible - schema_min) / min_visible * 100, 1)
            note = (
                f"Cena w danych strukturalnych ({schema_min}) jest o {roznica}% NIŻSZA niż najniższa "
                f"cena widoczna na stronie ({min_visible}). Wyszukiwarka i modele AI pokażą cenę, "
                "której użytkownik nie zobaczy - typowy skutek wysyłania do Schema ceny hurtowej B2B."
            )
        elif schema_min > (visible.get("max_visible") or min_visible) * (1 + PRICE_DISCREPANCY_TOLERANCE):
            status = "warning"
            note = (
                f"Cena w danych strukturalnych ({schema_min}) jest wyższa niż jakakolwiek cena widoczna "
                f"na stronie (maks. {visible.get('max_visible')}) - dane zaniżają atrakcyjność oferty."
            )
        else:
            status = "ok"
            note = f"Cena w danych strukturalnych ({schema_min}) jest zgodna z ceną widoczną na stronie."

        return self._make_metric(
            "structure", "price_discrepancy",
            {
                "schema_prices": sorted(set(schema_prices)),
                "visible_min": min_visible,
                "visible_max": visible.get("max_visible"),
                "note": note,
            },
            status,
            current_value=(
                f"Schema: {sorted(set(schema_prices))}; widoczne na stronie: {visible.get('visible_values')}"
            ),
        )

    def _evaluate_schema_data_hygiene(self, data: dict) -> dict:
        """Czystość danych w JSON-LD: wycieki środowisk, podwójne kodowanie, sufiksy SEO.

        Wszystkie trzy usterki mają wspólną cechę: dane strukturalne są generowane
        maszynowo i nikt ich nie ogląda, więc błąd potrafi żyć miesiącami, zanieczyszczając
        to, co wyszukiwarka i modele AI wiedzą o firmie.
        """
        entities = self._schema_entities(data)
        if not entities:
            return self._make_metric(
                "structure", "schema_data_hygiene",
                {"env_leaks": [], "double_encoded": [], "seo_suffixes": [], "note":
                 "Brak danych strukturalnych JSON-LD - nie ma czego sprawdzać."},
                "info",
                current_value="(brak encji JSON-LD)",
                generate_recommendation=False,
            )

        env_leaks: list[str] = []
        double_encoded: list[str] = []
        seo_suffixes: list[str] = []

        for entity in entities:
            for prop in ("url", "sameAs", "image", "@id", "logo", "contentUrl"):
                for value in self._iter_string_values(entity.get(prop)):
                    lowered = value.lower()
                    if any(marker in lowered for marker in SCHEMA_ENV_LEAK_MARKERS):
                        env_leaks.append(f"{prop}: {value[:90]}")

            for prop in ("description", "name", "text", "headline", "articleBody"):
                for value in self._iter_string_values(entity.get(prop)):
                    if any(marker in value for marker in SCHEMA_DOUBLE_ENCODING_MARKERS):
                        double_encoded.append(f"{prop}: {value[:90]}")

            for prop in ("name", "headline"):
                for value in self._iter_string_values(entity.get(prop)):
                    if self._looks_like_seo_suffix(value, data.get("url", "")):
                        seo_suffixes.append(f"{prop}: {value[:90]}")

        problemy = len(env_leaks) + len(double_encoded) + len(seo_suffixes)
        if env_leaks:
            status = "error"
            note = (
                f"Dane strukturalne zawierają adresy środowiska testowego ({len(env_leaks)}): "
                f"{env_leaks[0]}. Takie adresy trafiają do wyszukiwarki jako oficjalne zasoby firmy."
            )
        elif double_encoded:
            status = "warning"
            note = (
                f"Wykryto podwójnie zakodowane encje HTML w {len(double_encoded)} wartościach - "
                "tekst wyświetli się z artefaktami typu &amp;amp; zamiast znaku."
            )
        elif seo_suffixes:
            status = "warning"
            note = (
                f"Nazwy w danych strukturalnych zawierają sufiks z nazwą domeny ({len(seo_suffixes)}): "
                f"{seo_suffixes[0]}. Do Schema powinna trafiać czysta nazwa produktu."
            )
        else:
            status = "ok"
            note = "Dane strukturalne są czyste: bez adresów testowych, podwójnego kodowania i sufiksów SEO."

        return self._make_metric(
            "structure", "schema_data_hygiene",
            {
                "env_leaks": env_leaks,
                "double_encoded": double_encoded,
                "seo_suffixes": seo_suffixes,
                "issues": problemy,
                "note": note,
            },
            status,
            current_value="; ".join((env_leaks + double_encoded + seo_suffixes)[:5]) or "(dane bez zastrzeżeń)",
        )

    def _iter_string_values(self, value):
        """Wszystkie wartości tekstowe właściwości - JSON-LD dopuszcza string, listę i obiekt."""
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from self._iter_string_values(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in ("@type", "@context"):
                    yield from self._iter_string_values(item)

    def _looks_like_seo_suffix(self, value: str, page_url: str) -> bool:
        """Czy nazwa kończy się separatorem i nazwą domeny (np. "Kosz | FreshGift.pl")."""
        from urllib.parse import urlparse

        host = urlparse(page_url or "").netloc.lower().replace("www.", "")
        brand = host.split(".")[0] if host else ""
        if not brand:
            return False

        for separator in SCHEMA_NAME_SEPARATORS:
            if separator in value:
                suffix = value.rsplit(separator, 1)[-1].strip().lower()
                if brand and brand in suffix:
                    return True
        return False

    def _evaluate_ecommerce_completeness(self, data: dict) -> dict:
        """Kompletność danych e-commerce wymagana przez AI Overviews i rich results.

        Karta produktu bez oceny, marki, danych wysyłki i polityki zwrotów nie kwalifikuje
        się do rozszerzonych wyników Google i rzadko trafia do odpowiedzi generatywnych -
        modelowi brakuje wtedy informacji, których oczekuje pytający ("ile kosztuje wysyłka",
        "czy mogę zwrócić").
        """
        entities = self._schema_entities(data)
        products = self._entities_of_type(entities, "Product")
        collections = self._entities_of_type(entities, "CollectionPage")

        if not products and not collections:
            return self._make_metric(
                "structure", "ecommerce_completeness",
                {"missing": [], "note":
                 "Strona nie deklaruje typu Product ani CollectionPage - test dotyczy stron sklepowych."},
                "info",
                current_value="(brak encji e-commerce w JSON-LD)",
                generate_recommendation=False,
            )

        braki: list[str] = []

        for product in products:
            if not (product.get("aggregateRating") or product.get("review")):
                braki.append("Product: brak AggregateRating ani Review")
            brand = product.get("brand")
            if not brand or (isinstance(brand, dict) and not (brand.get("name") or brand.get("@id"))):
                braki.append("Product: właściwość brand jest pusta lub nie istnieje")
            oferty = [product.get("offers")] if isinstance(product.get("offers"), dict) else (product.get("offers") or [])
            oferty = [o for o in oferty if isinstance(o, dict)]
            if not any(o.get("shippingDetails") for o in oferty) and not product.get("shippingDetails"):
                braki.append("Product: brak OfferShippingDetails")
            if not any(o.get("hasMerchantReturnPolicy") for o in oferty) and not product.get("hasMerchantReturnPolicy"):
                braki.append("Product: brak MerchantReturnPolicy")

        for collection in collections:
            item_list = collection.get("mainEntity") or collection.get("hasPart") or collection.get("itemListElement")
            elementy = item_list.get("itemListElement") if isinstance(item_list, dict) else item_list
            if not elementy:
                braki.append("CollectionPage: brak obiektu ItemList z produktami (pusta kolekcja)")

        if not braki:
            status = "ok"
            note = (
                f"Dane e-commerce są kompletne ({len(products)} Product, {len(collections)} CollectionPage) - "
                "strona kwalifikuje się do rozszerzonych wyników i odpowiedzi AI."
            )
        elif len(braki) >= ECOMMERCE_CRITICAL_MISSING:
            status = "error"
            note = f"Brakuje {len(braki)} kluczowych właściwości e-commerce: " + "; ".join(braki[:4]) + "."
        else:
            status = "warning"
            note = f"Niekompletne dane e-commerce: " + "; ".join(braki) + "."

        return self._make_metric(
            "structure", "ecommerce_completeness",
            {
                "products": len(products),
                "collections": len(collections),
                "missing": braki,
                "note": note,
            },
            status,
            current_value="; ".join(braki) or "(komplet wymaganych właściwości)",
        )

    # ------------------------------------------------------------------
    # MODUŁ: automatyczna ewaluacja E-E-A-T
    # ------------------------------------------------------------------
    def _evaluate_authorship_depth(self, data: dict) -> dict:
        """Głębia sygnałów autorstwa: kim jest autor i czym to potwierdza.

        Google ocenia "Experience" i "Expertise" po tym, czy da się ustalić kompetencje
        autora. Samo imię i nazwisko w stopce nie niesie tej informacji - potrzebne jest
        stanowisko lub opis oraz powiązanie z profilem zewnętrznym (`sameAs`), które
        pozwala połączyć autora z jego dorobkiem poza witryną.
        """
        entities = self._schema_entities(data)
        persons = self._entities_of_type(entities, "Person")
        page_type = data.get("page_type", "generic")
        wymagane = page_type in EEAT_REQUIRED_PAGE_TYPES

        if not persons:
            if not wymagane:
                return self._make_metric(
                    "structure", "authorship_depth",
                    {"persons": 0, "complete": 0, "note":
                     "Strona nie deklaruje autora w danych strukturalnych - dla tego typu podstrony "
                     "to dopuszczalne (wymóg dotyczy przede wszystkim treści poradnikowych)."},
                    "info",
                    current_value="(brak encji Person w JSON-LD)",
                    generate_recommendation=False,
                )
            return self._make_metric(
                "structure", "authorship_depth",
                {"persons": 0, "complete": 0, "note":
                 "Treść poradnikowa bez encji Person w danych strukturalnych - wyszukiwarka nie ma "
                 "jak ustalić, kto jest autorem ani jakie ma kompetencje."},
                "warning",
                current_value="(brak encji Person w JSON-LD)",
            )

        braki: list[str] = []
        kompletni = 0
        for person in persons:
            imie = str(person.get("name") or "autor bez nazwy")
            ma_kompetencje = any(person.get(prop) for prop in AUTHOR_EXPERTISE_PROPERTIES)
            ma_powiazanie = bool(person.get("sameAs"))
            if ma_kompetencje and ma_powiazanie:
                kompletni += 1
                continue
            czego_brak = []
            if not ma_kompetencje:
                czego_brak.append("stanowiska/opisu (jobTitle lub description)")
            if not ma_powiazanie:
                czego_brak.append("powiązania zewnętrznego (sameAs)")
            braki.append(f"{imie}: brak {' i '.join(czego_brak)}")

        if kompletni == len(persons):
            status = "ok"
            note = (
                f"Autorstwo jest udokumentowane: {kompletni} autor(ów) ma opisane kompetencje "
                "i powiązanie z profilem zewnętrznym."
            )
        elif kompletni:
            status = "warning"
            note = f"Część autorów ma niepełne dane E-E-A-T ({len(braki)} z {len(persons)}): " + braki[0] + "."
        else:
            status = "warning"
            note = (
                "Autor jest podany, ale bez dowodów kompetencji: " + braki[0] +
                ". Samo imię i nazwisko nie wykazuje doświadczenia ani eksperckości."
            )

        return self._make_metric(
            "structure", "authorship_depth",
            {"persons": len(persons), "complete": kompletni, "missing": braki, "note": note},
            status,
            current_value="; ".join(braki[:5]) or f"Kompletni autorzy: {kompletni}/{len(persons)}",
        )

    def _evaluate_freshness_decay(self, data: dict) -> dict:
        """Sygnał odświeżenia treści: różnica między datePublished a dateModified.

        Treść opublikowana lata temu i nigdy nieaktualizowana traci wiarygodność - dla
        wyszukiwarki identyczne daty publikacji i modyfikacji po roku oznaczają, że nikt
        nie zweryfikował, czy informacje są nadal prawdziwe.
        """
        from datetime import date, datetime

        entities = self._schema_entities(data)
        published = modified = None
        for entity in entities:
            published = published or self._parse_schema_date(entity.get("datePublished"))
            modified = modified or self._parse_schema_date(entity.get("dateModified"))

        if not published:
            return self._make_metric(
                "structure", "freshness_decay",
                {"published": None, "modified": None, "note":
                 "Dane strukturalne nie zawierają daty publikacji - nie da się ocenić aktualności treści."},
                "info",
                current_value="(brak datePublished w JSON-LD)",
                generate_recommendation=False,
            )

        wiek_dni = (date.today() - published).days
        if modified and modified > published:
            status = "ok"
            note = (
                f"Treść ma sygnał odświeżenia: opublikowana {published.isoformat()}, "
                f"zaktualizowana {modified.isoformat()}."
            )
        elif wiek_dni > FRESHNESS_DECAY_DAYS:
            status = "warning"
            note = (
                f"Treść opublikowana {published.isoformat()} (ponad {wiek_dni // 365} lat temu) nie ma "
                "sygnału aktualizacji - dateModified jest identyczna z datą publikacji lub jej brak. "
                "Przegląd merytoryczny i aktualizacja daty wzmacniają wiarygodność."
            )
        else:
            status = "ok"
            note = f"Treść jest świeża: opublikowana {published.isoformat()} ({wiek_dni} dni temu)."

        return self._make_metric(
            "structure", "freshness_decay",
            {
                "published": published.isoformat(),
                "modified": modified.isoformat() if modified else None,
                "age_days": wiek_dni,
                "note": note,
            },
            status,
            current_value=(
                f"datePublished: {published.isoformat()}; "
                f"dateModified: {modified.isoformat() if modified else 'brak'}"
            ),
        )

    def _parse_schema_date(self, value):
        """Data z JSON-LD (ISO 8601, także z częścią czasową) na obiekt date."""
        from datetime import datetime

        if not isinstance(value, str) or not value.strip():
            return None
        tekst = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(tekst).date()
        except ValueError:
            try:
                return datetime.strptime(tekst[:10], "%Y-%m-%d").date()
            except ValueError:
                return None

    def _evaluate_external_sources(self, data: dict) -> dict:
        """Linki do źródeł zewnętrznych jako dowód weryfikacji faktów.

        Treść poradnikowa, która nie powołuje się na żadne źródło, jest dla wyszukiwarki
        i modeli AI twierdzeniem bez pokrycia. Linki do domen rządowych, edukacyjnych czy
        uznanych publikacji świadczą, że autor opiera się na zewnętrznej wiedzy.
        """
        outbound = data.get("outbound_links", {})
        page_type = data.get("page_type", "generic")
        zaufane = outbound.get("trusted", 0)
        followed = outbound.get("followed_trusted", 0)

        if page_type not in EEAT_REQUIRED_PAGE_TYPES:
            return self._make_metric(
                "structure", "external_sources",
                {"outbound": outbound.get("total", 0), "trusted": zaufane, "note":
                 "Test dotyczy treści poradnikowych i blogowych - na tym typie podstrony "
                 "powoływanie się na źródła zewnętrzne nie jest wymagane."},
                "info",
                current_value=f"Linki wychodzące: {outbound.get('total', 0)} (zaufane: {zaufane})",
                generate_recommendation=False,
            )

        if zaufane >= MIN_TRUSTED_SOURCES and followed:
            status = "ok"
            note = (
                f"Treść powołuje się na {zaufane} źródeł zewnętrznych "
                f"({', '.join(outbound.get('trusted_hosts', [])[:3])}) - sygnał weryfikacji faktów."
            )
        elif zaufane:
            status = "warning"
            note = (
                f"Wszystkie {zaufane} linków do źródeł zewnętrznych mają atrybut nofollow - "
                "wyszukiwarka nie odczyta ich jako świadomego powołania się na źródło."
            )
        else:
            status = "warning"
            note = (
                "Treść poradnikowa nie zawiera linków do zewnętrznych źródeł (domeny rządowe, "
                "edukacyjne, uznane publikacje) - brak dowodu weryfikacji przedstawionych faktów."
            )

        return self._make_metric(
            "structure", "external_sources",
            {
                "outbound": outbound.get("total", 0),
                "trusted": zaufane,
                "followed_trusted": followed,
                "hosts": outbound.get("trusted_hosts", []),
                "note": note,
            },
            status,
            current_value=", ".join(outbound.get("trusted_hosts", [])) or "(brak linków do źródeł zewnętrznych)",
        )

    # ------------------------------------------------------------------
    # MODUŁ: GEO Suppression - treść ukryta przed modelami językowymi
    # ------------------------------------------------------------------
    def _evaluate_hidden_content(self, data: dict) -> dict:
        """Treść ukryta stylem CSS, niedostępna dla crawlerów i modeli językowych.

        Opinie klientów, FAQ i specyfikacje schowane pod "pokaż więcej" są dla robotów
        treścią drugiej kategorii albo nie istnieją wcale. To najkosztowniejszy rodzaj
        straty w GEO: materiał, który najbardziej przekonuje modele (doświadczenia
        klientów), jest dokładnie tym, co najczęściej zwija się do przycisku.
        """
        hidden = data.get("hidden_content", {})
        udzial = hidden.get("hidden_share", 0.0)
        bloki = hidden.get("blocks_count", 0)
        slowa = hidden.get("hidden_words", 0)

        if not bloki:
            return self._make_metric(
                "structure", "hidden_content",
                {"blocks": 0, "hidden_words": 0, "share": 0.0, "note":
                 "Nie wykryto dużych bloków treści ukrytych stylem CSS - treść jest dostępna dla robotów."},
                "ok",
                current_value="(brak ukrytych bloków treści)",
                generate_recommendation=False,
            )

        przyklady = "; ".join(
            f"<{b['tag']} class=\"{b['class']}\">: {b['words']} słów"
            for b in hidden.get("blocks", [])[:3]
        )

        if udzial >= HIDDEN_CONTENT_CRITICAL_SHARE:
            status = "error"
            note = (
                f"KRYTYCZNE dla widoczności w AI: {int(udzial * 100)}% treści strony ({slowa} słów "
                f"w {bloki} blokach) jest ukryte stylem CSS. Modele językowe nie zobaczą tej treści "
                "- to zwykle opinie klientów, FAQ lub specyfikacje zwinięte pod \"pokaż więcej\"."
            )
        elif udzial >= HIDDEN_CONTENT_WARNING_SHARE:
            status = "warning"
            note = (
                f"{int(udzial * 100)}% treści ({slowa} słów w {bloki} blokach) jest ukryte stylem CSS. "
                "Rozważ renderowanie tej treści w HTML i zwijanie jej dopiero po stronie klienta."
            )
        else:
            status = "warning"
            note = (
                f"Wykryto {bloki} ukrytych bloków treści ({slowa} słów). Sprawdź, czy nie są to "
                "opinie, FAQ lub specyfikacje istotne dla widoczności w odpowiedziach AI."
            )

        return self._make_metric(
            "structure", "hidden_content",
            {
                "blocks": bloki,
                "hidden_words": slowa,
                "visible_words": hidden.get("visible_words", 0),
                "share": udzial,
                "examples": hidden.get("blocks", []),
                "note": note,
            },
            status,
            current_value=przyklady or f"Ukrytych bloków: {bloki}",
        )

    # ------------------------------------------------------------------
    # Testy autorskie - zabezpieczenie przed wzorcami usterek z audytów
    # ------------------------------------------------------------------
    def _evaluate_heading_visibility(self, data: dict) -> dict:
        """Nagłówki obecne w HTML, ale niewidoczne w interfejsie.

        Wzorzec z audytu: pod prawidłowym H1 kryje się ukryty komunikat ("Nie znaleziono
        produktów") w znaczniku nagłówka, po którym następuje H2. Użytkownik widzi
        poprawną stronę, a robot - hierarchię z nieistniejącym poziomem i sprzecznym
        komunikatem o braku treści.
        """
        visibility = data.get("heading_visibility", {})
        ukryte = visibility.get("hidden_headings", [])

        if not ukryte:
            return self._make_metric(
                "structure", "heading_visibility",
                {"hidden_headings": [], "note":
                 "Wszystkie nagłówki obecne w HTML są widoczne dla użytkownika."},
                "ok",
                current_value="(brak ukrytych nagłówków)",
                generate_recommendation=False,
            )

        opisy = [f"{h['tag']}: {h['text']}" for h in ukryte]
        komunikaty_o_braku = [
            h for h in ukryte
            if any(fraza in h["text"].lower() for fraza in ("nie znaleziono", "brak wyników", "brak produktów"))
        ]

        if komunikaty_o_braku:
            status = "error"
            note = (
                f"Ukryty nagłówek z komunikatem o braku treści ({komunikaty_o_braku[0]['tag']}: "
                f"\"{komunikaty_o_braku[0]['text']}\") jest niewidoczny dla użytkownika, ale robot "
                "odczytuje go jako treść strony - sprzeczny sygnał o zawartości podstrony."
            )
        else:
            status = "warning"
            note = (
                f"Wykryto {len(ukryte)} nagłówków ukrytych stylem CSS - dla robota tworzą poziomy "
                "hierarchii, które w interfejsie nie istnieją."
            )

        return self._make_metric(
            "structure", "heading_visibility",
            {"hidden_headings": ukryte, "hidden_count": len(ukryte), "note": note},
            status,
            current_value="; ".join(opisy[:5]),
        )

    def _evaluate_schema_html_parity(self, data: dict) -> dict:
        """Zgodność deklaracji w danych strukturalnych z rzeczywistą treścią HTML.

        Wzorzec z audytu: strona deklaruje FAQPage w JSON-LD, ale w HTML nie ma żadnej
        semantycznej struktury pytań i odpowiedzi. Google traktuje takie dane jako
        niezgodne z treścią widoczną dla użytkownika, co grozi ręczną karą - a model
        językowy i tak nie znajdzie w DOM materiału, który Schema obiecuje.
        """
        entities = self._schema_entities(data)
        rozbieznosci: list[str] = []

        deklaruje_faq = bool(self._entities_of_type(entities, "FAQPage")) or bool(
            self._entities_of_type(entities, "Question")
        )
        if deklaruje_faq and not data.get("faq_detected"):
            rozbieznosci.append(
                "Schema deklaruje FAQPage, ale w HTML nie wykryto sekcji pytań i odpowiedzi"
            )

        produkty = self._entities_of_type(entities, "Product")
        if produkty and not (data.get("visible_prices", {}).get("min_visible")):
            rozbieznosci.append(
                "Schema deklaruje Product, ale na stronie nie widać ceny"
            )

        kolekcje = self._entities_of_type(entities, "CollectionPage")
        if kolekcje and data.get("word_count", 0) < COLLECTION_MIN_WORDS:
            rozbieznosci.append(
                "Schema deklaruje CollectionPage, ale strona praktycznie nie zawiera treści"
            )

        if not entities:
            return self._make_metric(
                "structure", "schema_html_parity",
                {"mismatches": [], "note":
                 "Brak danych strukturalnych - nie ma deklaracji do skonfrontowania z HTML."},
                "info",
                current_value="(brak encji JSON-LD)",
                generate_recommendation=False,
            )

        if rozbieznosci:
            status = "error"
            note = (
                "Dane strukturalne obiecują treść, której nie ma w HTML: " +
                "; ".join(rozbieznosci) + ". Google uznaje to za dane niezgodne z zawartością strony."
            )
        else:
            status = "ok"
            note = "Deklaracje w danych strukturalnych mają pokrycie w treści HTML."

        return self._make_metric(
            "structure", "schema_html_parity",
            {"mismatches": rozbieznosci, "note": note},
            status,
            current_value="; ".join(rozbieznosci) or "(deklaracje zgodne z treścią)",
        )

    def _evaluate_placeholder_content(self, data: dict) -> dict:
        """Teksty zastępcze pozostawione na produkcji.

        "Lorem ipsum", "TODO" czy "opis w przygotowaniu" w treści lub danych
        strukturalnych to sygnał niedokończonej strony. Wyszukiwarka indeksuje je jak
        każdą inną treść, a model językowy może je zacytować jako opis oferty.
        """
        znalezione: list[str] = []

        # Treść widoczna na stronie (analizowana w scraperze na pełnym tekście) - to tam
        # najczęściej zostaje "lorem ipsum", a nie w nagłówkach czy meta tagach.
        for trafienie in data.get("placeholder_hits", []):
            znalezione.append(f"treść: {trafienie['context'][:70]}")

        for heading_list in (data.get("headings") or {}).values():
            for tekst in heading_list:
                dopasowanie = self._find_placeholder(tekst)
                if dopasowanie:
                    znalezione.append(f"nagłówek: {tekst[:70]}")

        for pole in ("title", "meta_description"):
            wartosc = data.get(pole)
            if wartosc and self._find_placeholder(wartosc):
                znalezione.append(f"{pole}: {str(wartosc)[:70]}")

        for entity in self._schema_entities(data):
            for prop in ("name", "description", "headline"):
                for wartosc in self._iter_string_values(entity.get(prop)):
                    if self._find_placeholder(wartosc):
                        znalezione.append(f"Schema.{prop}: {wartosc[:70]}")

        if not znalezione:
            return self._make_metric(
                "seo", "placeholder_content",
                {"found": [], "note": "Nie wykryto tekstów zastępczych w treści ani danych strukturalnych."},
                "ok",
                current_value="(brak tekstów zastępczych)",
                generate_recommendation=False,
            )

        return self._make_metric(
            "seo", "placeholder_content",
            {"found": znalezione, "count": len(znalezione), "note":
             f"Na stronie pozostały teksty zastępcze ({len(znalezione)}): {znalezione[0]}. "
             "Wyszukiwarka indeksuje je jak zwykłą treść, a modele AI mogą je zacytować."},
            "error",
            current_value="; ".join(znalezione[:5]),
        )

    def _find_placeholder(self, text) -> str | None:
        if not isinstance(text, str):
            return None
        lowered = text.lower()
        return next((marker for marker in PLACEHOLDER_MARKERS if marker in lowered), None)

    def _evaluate_schema_validity(self, data: dict) -> dict:
        """AI & GEO: poprawność składniowa JSON-LD i pokrycie typów istotnych dla AI.

        Uzupełnia `_evaluate_schema_page_type` (który sprawdza, czy typ strony pasuje
        do jej zawartości) o dwie rzeczy, których tamten test nie łapie:
          * bloki JSON-LD z błędem składni - dla wyszukiwarki i modelu językowego są
            niewidoczne, więc cichy błąd parsowania kosztuje całe oznaczenie strony,
          * obecność typów, po których modele AI budują odpowiedzi o firmie i ofercie
            (Organization, SoftwareApplication, FAQPage, Product).
        """
        schema = data.get("schema", {})
        blocks = schema.get("blocks_found", 0)
        parse_errors = schema.get("parse_errors", 0)
        types_found = set(schema.get("types_found", []))
        present = [t for t in AI_RELEVANT_SCHEMA_TYPES if t in types_found]
        missing = [t for t in AI_RELEVANT_SCHEMA_TYPES if t not in types_found]

        if parse_errors:
            status = "error"
            note = (
                f"{parse_errors} z {blocks} bloków JSON-LD ma błąd składni - wyszukiwarki i modele AI "
                "całkowicie je pomijają."
            )
        elif not blocks:
            status = "error"
            note = "Strona nie zawiera żadnych danych strukturalnych JSON-LD (<script type=\"application/ld+json\">)."
        elif not present:
            status = "warning"
            note = (
                "JSON-LD jest poprawny składniowo, ale nie zawiera żadnego z typów istotnych dla AI "
                f"({', '.join(AI_RELEVANT_SCHEMA_TYPES)})."
            )
        elif missing:
            status = "warning"
            note = (
                f"Wykryto poprawne typy: {', '.join(present)}. Brakuje jeszcze: {', '.join(missing)} - "
                "ich dodanie zwiększa szansę na cytowanie w odpowiedziach AI."
            )
        else:
            status = "ok"
            note = f"JSON-LD jest poprawny i zawiera wszystkie kluczowe typy: {', '.join(present)}."

        current_value = (
            f"Bloki JSON-LD: {blocks} (błędy składni: {parse_errors}). "
            f"Wykryte typy: {', '.join(sorted(types_found)) or 'brak'}."
        )
        return self._make_metric(
            "structure",
            "schema_validity",
            {
                "blocks_found": blocks,
                "parse_errors": parse_errors,
                "types_present": present,
                "types_missing": missing,
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_twitter_cards(self, data: dict) -> dict:
        """Social Graph: obecność i kompletność tagów Twitter Card (X).

        `twitter:title`/`description`/`image` są opcjonalne, jeśli strona ma
        odpowiedniki Open Graph - X używa ich wtedy jako fallbacku. Brakiem, który
        realnie psuje podgląd linku, jest dopiero brak `twitter:card` (typ karty)
        oraz jednoczesny brak obrazka w obu standardach.
        """
        twitter = data.get("twitter_card", {})
        tags = twitter.get("tags", {})
        og = data.get("open_graph", {})

        has_image = bool(tags.get("image") or og.get("image"))
        card_type = twitter.get("card_type")

        if not tags and not og:
            status = "error"
            note = (
                "Brak tagów Twitter Card i Open Graph - link udostępniony w mediach społecznościowych "
                "wyświetli się jako goły adres URL, bez tytułu i miniatury."
            )
        elif not card_type:
            status = "warning"
            note = (
                "Brak tagu twitter:card - X nie wie, jakiego typu podgląd wyświetlić "
                "(zalecane: summary_large_image)."
            )
        elif not has_image:
            status = "warning"
            note = "Karta Twitter jest zadeklarowana, ale brakuje obrazka (twitter:image ani og:image)."
        else:
            status = "ok"
            note = f"Karta Twitter jest kompletna (typ: {card_type}, obrazek podglądu obecny)."

        current_value = (
            "; ".join(f"twitter:{key}={value}" for key, value in sorted(tags.items()))
            if tags
            else "(brak tagów twitter:*)"
        )
        return self._make_metric(
            "seo",
            "twitter_cards",
            {
                "tags": tags,
                "card_type": card_type,
                "has_image": has_image,
                "falls_back_to_og": bool(og and not tags),
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_favicon(self, data: dict) -> dict:
        """Social Graph: ikona witryny widoczna w karcie przeglądarki i zakładkach."""
        favicon = data.get("favicon", {})
        declared = favicon.get("declared", [])

        if not declared:
            status = "warning"
            note = (
                "Strona nie deklaruje ikony witryny w <head>. Przeglądarki spróbują pobrać domyślny "
                "/favicon.ico, ale jawna deklaracja jest pewniejsza i pozwala podać wersje HD."
            )
            current_value = "(brak <link rel=\"icon\"> w sekcji <head>)"
        elif not favicon.get("has_apple_touch_icon"):
            status = "ok"
            note = (
                f"Ikona witryny jest zadeklarowana ({len(declared)} wariant(ów)). Warto dodać jeszcze "
                "apple-touch-icon dla ekranu głównego iOS."
            )
            current_value = "; ".join(f'rel="{item["rel"]}" -> {item["href"]}' for item in declared)
        else:
            status = "ok"
            note = f"Ikona witryny jest poprawnie zadeklarowana ({len(declared)} wariant(ów), w tym apple-touch-icon)."
            current_value = "; ".join(f'rel="{item["rel"]}" -> {item["href"]}' for item in declared)

        return self._make_metric(
            "technical",
            "favicon",
            {
                "declared_count": len(declared),
                "declared": declared,
                "has_apple_touch_icon": favicon.get("has_apple_touch_icon", False),
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_thin_content(self, data: dict) -> dict:
        """Content Quality: czy strona ma wystarczającą objętość treści.

        Poniżej {THIN_CONTENT_MIN_WORDS} słów strona jest zwykle zbyt uboga, żeby
        konkurować w wynikach wyszukiwania i zostać uznana za wartościowe źródło przez
        modele językowe. Próg liczony jest na widocznym tekście (bez skryptów i
        stylów) - patrz `SEOScraper._analyze_js_rendering`.
        """
        word_count = data.get("word_count", 0)

        if word_count == 0:
            status = "error"
            note = (
                "Nie wykryto żadnej widocznej treści tekstowej w surowym HTML - strona jest pusta "
                "dla robotów, które nie wykonują JavaScriptu."
            )
        elif word_count < THIN_CONTENT_CRITICAL_WORDS:
            status = "error"
            note = f"Bardzo uboga treść: {word_count} słów (rekomendowane minimum to {THIN_CONTENT_MIN_WORDS})."
        elif word_count < THIN_CONTENT_MIN_WORDS:
            status = "warning"
            note = f"Uboga treść: {word_count} słów - poniżej rekomendowanego minimum {THIN_CONTENT_MIN_WORDS} słów."
        else:
            status = "ok"
            note = f"Objętość treści jest wystarczająca ({word_count} słów)."

        return self._make_metric(
            "seo",
            "thin_content",
            {"word_count": word_count, "threshold": THIN_CONTENT_MIN_WORDS, "note": note},
            status,
            current_value=f"Liczba słów widocznej treści: {word_count}",
        )

    def _evaluate_title(self, data: dict) -> dict:
        title = data.get("title")
        length = data.get("title_length", 0)
        # Brak pola oznacza wynik sprzed wprowadzenia zamienników - traktujemy go jak
        # tytuł z właściwego tagu, żeby starsze dane nie zaczęły nagle zgłaszać ostrzeżeń.
        source = data.get("title_source") or "title"
        current_value = title if title else "(brak tagu <title>)"

        if not title:
            status, note = "error", "Brak tagu <title>."
        elif source != "title":
            # Tytuł znaleziony wyłącznie w Open Graph / Twitter Card. Treść istnieje,
            # więc to nie jest błąd krytyczny, ale Google wyświetla w wynikach <title> -
            # zgłoszenie "OK" ukrywałoby realny brak.
            status, note = (
                "warning",
                f"Brak tagu <title> - tytuł odczytany z zamiennika {source}. "
                "Wyszukiwarka wyświetla w wynikach <title>, więc dodaj go w sekcji <head>.",
            )
            current_value = f"{title}  [źródło: {source}]"
        elif length < TITLE_MIN_LENGTH or length > TITLE_MAX_LENGTH:
            status, note = (
                "warning",
                f"Długość tytułu ({length} znaków) poza zalecanym zakresem "
                f"{TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH}.",
            )
        else:
            status, note = "ok", "Długość tytułu jest prawidłowa."

        return self._make_metric(
            "seo", "title",
            {"value": title, "length": length, "source": source, "note": note}, status,
            current_value=current_value,
        )

    def _evaluate_description(self, data: dict) -> dict:
        description = data.get("meta_description")
        length = data.get("meta_description_length", 0)
        source = data.get("meta_description_source") or "description"
        current_value = description if description else "(brak meta description)"

        if not description:
            status, note = "error", "Brak meta description."
        elif source != "description":
            status, note = (
                "warning",
                f'Brak <meta name="description"> - opis odczytany z zamiennika {source}. '
                "Wyszukiwarka buduje fragment wyniku z meta description, więc dodaj ją w <head>.",
            )
            current_value = f"{description}  [źródło: {source}]"
        elif length < DESCRIPTION_MIN_LENGTH or length > DESCRIPTION_MAX_LENGTH:
            status, note = (
                "warning",
                f"Długość opisu ({length} znaków) poza zalecanym zakresem "
                f"{DESCRIPTION_MIN_LENGTH}-{DESCRIPTION_MAX_LENGTH}.",
            )
        else:
            status, note = "ok", "Długość meta description jest prawidłowa."

        return self._make_metric(
            "seo", "meta_description",
            {"value": description, "length": length, "source": source, "note": note}, status,
            current_value=current_value,
        )

    def _evaluate_h1(self, data: dict) -> dict:
        """Ocena nagłówka H1 liczona na nagłówkach Z TREŚCIĄ.

        Pusty `<h1></h1>` istnieje w drzewie DOM, ale nie niesie żadnej informacji dla
        wyszukiwarki - liczenie go jako poprawnego H1 tworzyło sprzeczność z testem
        hierarchii nagłówków (`_evaluate_heading_order`), który zgłaszał puste nagłówki
        jako problem, podczas gdy ten test raportował "Struktura H1 jest prawidłowa".
        Oba testy opierają się teraz na tej samej definicji nagłówka, który się liczy.
        """
        h1_headings = data.get("headings", {}).get("h1", [])
        non_empty = data.get("h1_non_empty", [text for text in h1_headings if text])
        empty_count = data.get("h1_empty_count", len(h1_headings) - len(non_empty))

        current_value = "; ".join(non_empty) if non_empty else "(brak nagłówka H1 z treścią)"
        if empty_count:
            current_value += f" [pustych znaczników H1: {empty_count}]"

        if not non_empty and empty_count:
            status = "error"
            note = f"Nagłówek H1 jest pusty ({empty_count} znacznik(ów) H1 bez treści)."
        elif not non_empty:
            status, note = "error", "Brak nagłówka H1."
        elif len(non_empty) > 1:
            status = "warning"
            note = f"Wykryto {len(non_empty)} nagłówków H1 z treścią (zalecany dokładnie 1)."
        elif empty_count:
            status = "warning"
            note = (
                f"Strona ma poprawny nagłówek H1, ale obok niego występuje {empty_count} "
                "pusty znacznik H1 - usuń go, żeby nie rozmywał struktury dokumentu."
            )
        else:
            status, note = "ok", "Struktura H1 jest prawidłowa."
        return self._make_metric(
            "technical",
            "h1_structure",
            {
                "count": len(non_empty),
                "empty_count": empty_count,
                "headings": h1_headings,
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_canonical(self, data: dict) -> dict:
        canonical = data.get("canonical")
        current_value = canonical if canonical else "(brak znacznika canonical)"
        status = "ok" if canonical else "warning"
        note = "Canonical ustawiony poprawnie." if canonical else "Brak znacznika canonical."
        return self._make_metric(
            "technical", "canonical", {"value": canonical, "note": note}, status,
            current_value=current_value,
        )

    def _evaluate_open_graph(self, data: dict) -> dict:
        og = data.get("open_graph", {})
        required = {"title", "description", "image"}
        missing = required - og.keys()
        current_value = (
            "; ".join(f"og:{key}={value}" for key, value in og.items())
            if og
            else "(brak tagów Open Graph)"
        )
        status = "ok" if not missing else "warning"
        note = (
            "Wszystkie kluczowe tagi Open Graph obecne."
            if not missing
            else f"Brakujące tagi Open Graph: {', '.join(sorted(missing))}."
        )
        return self._make_metric(
            "seo", "open_graph", {"value": og, "note": note}, status, current_value=current_value
        )

    def _evaluate_images(self, data: dict) -> dict:
        total = data.get("images_total", 0)
        without_alt = data.get("images_without_alt", 0)
        without_alt_examples = data.get("images_without_alt_examples", [])
        current_value = (
            "; ".join(without_alt_examples) if without_alt_examples else "(wszystkie obrazki mają atrybut ALT)"
        )
        if total == 0:
            status, note = "ok", "Brak obrazków na stronie."
        elif without_alt == 0:
            status, note = "ok", "Wszystkie obrazki mają atrybut ALT."
        elif without_alt / total > 0.5:
            status, note = "error", f"{without_alt}/{total} obrazków bez atrybutu ALT."
        else:
            status, note = "warning", f"{without_alt}/{total} obrazków bez atrybutu ALT."
        return self._make_metric(
            "performance",
            "images_alt",
            {
                "total": total,
                "with_alt": data.get("images_with_alt", 0),
                "without_alt": without_alt,
                "without_alt_examples": without_alt_examples,
                "note": note,
            },
            status,
            current_value=current_value,
        )

    # ------------------------------------------------------------------
    # Nowe testy strukturalne / SEO / GEO / E-E-A-T
    # ------------------------------------------------------------------
    def _evaluate_schema_page_type(self, data: dict) -> dict:
        """Waliduje, czy podstrona zawiera typy Schema.org oczekiwane dla jej wykrytego
        typu (Strona główna / Produkt / Artykuł / Kategoria / Ogólna) - zarówno z bloków
        JSON-LD, jak i Microdata (itemscope/itemtype)."""
        page_type = data.get("page_type", "generic")
        page_type_label = PAGE_TYPE_LABELS.get(page_type, PAGE_TYPE_LABELS["generic"])
        expected_groups = EXPECTED_SCHEMA_BY_PAGE_TYPE.get(page_type, EXPECTED_SCHEMA_BY_PAGE_TYPE["generic"])
        types_found = set(data.get("schema", {}).get("types_found", []))

        matched_groups = [group for group in expected_groups if group & types_found]
        missing_groups = [group for group in expected_groups if not (group & types_found)]

        def describe(group: set[str]) -> str:
            return " lub ".join(sorted(group))

        expected_desc = "; ".join(describe(g) for g in expected_groups)

        if not missing_groups:
            status = "ok"
            note = f"{page_type_label}: wykryto wszystkie oczekiwane typy Schema.org ({expected_desc})."
        elif matched_groups:
            status = "warning"
            missing_desc = "; ".join(describe(g) for g in missing_groups)
            note = f"{page_type_label}: brakuje części oczekiwanych danych strukturalnych ({missing_desc})."
        else:
            status = "error"
            note = f"{page_type_label}: brak jakichkolwiek oczekiwanych typów Schema.org ({expected_desc})."

        value = {
            "page_type": page_type,
            "page_type_label": page_type_label,
            "types_found": sorted(types_found),
            "expected_groups": [sorted(g) for g in expected_groups],
            "missing_groups": [sorted(g) for g in missing_groups],
            "note": note,
        }
        current_value = ", ".join(sorted(types_found)) if types_found else "(brak danych strukturalnych Schema.org)"
        return self._make_metric("structure", "schema_page_type", value, status, current_value=current_value)

    def _evaluate_schema_breadcrumbs(self, data: dict) -> dict:
        """Uniwersalna reguła: każda podstrona powinna mieć dane strukturalne BreadcrumbList,
        niezależnie od jej typu."""
        types_found = set(data.get("schema", {}).get("types_found", []))
        has_breadcrumbs = "BreadcrumbList" in types_found

        if has_breadcrumbs:
            status, note = "ok", "Wykryto dane strukturalne BreadcrumbList."
        else:
            status, note = "warning", "Brak danych strukturalnych BreadcrumbList na stronie."

        value = {"has_breadcrumbs": has_breadcrumbs, "note": note}
        current_value = ", ".join(sorted(types_found)) if types_found else "(brak danych strukturalnych Schema.org)"
        return self._make_metric("structure", "schema_breadcrumbs", value, status, current_value=current_value)

    def _evaluate_schema_faq(self, data: dict) -> dict:
        """Uniwersalna reguła: jeśli na stronie wykryto (dynamicznie, po treści) sekcję FAQ,
        ale w kodzie brakuje danych strukturalnych FAQPage, zgłaszamy ostrzeżenie."""
        faq_detected = bool(data.get("faq_detected"))
        types_found = set(data.get("schema", {}).get("types_found", []))
        has_faq_schema = "FAQPage" in types_found

        if not faq_detected:
            status, note = "ok", "Nie wykryto sekcji FAQ na stronie."
        elif has_faq_schema:
            status, note = "ok", "Wykryto sekcję FAQ oraz odpowiadające jej dane strukturalne FAQPage."
        else:
            status, note = (
                "warning",
                "Wykryto sekcję pytań (FAQ) na stronie, ale brakuje danych strukturalnych FAQPage.",
            )

        value = {"faq_detected": faq_detected, "has_faq_schema": has_faq_schema, "note": note}
        current_value = (
            "Wykryto sekcję FAQ w treści strony." if faq_detected else "Nie wykryto sekcji FAQ w treści strony."
        )
        return self._make_metric("structure", "schema_faq", value, status, current_value=current_value)

    def _evaluate_heading_order(self, data: dict) -> dict:
        """Poprawność hierarchii nagłówków: kolejność względem H1, puste nagłówki i
        przeskoki poziomów (H1 -> H3 z pominięciem H2).

        Wszystkie trzy problemy dotyczą tej samej rzeczy - logicznej struktury
        dokumentu - więc raportujemy je w JEDNEJ karcie testu, zamiast rozbijać na
        osobne pozycje, które użytkownik i tak naprawia jedną zmianą w szablonie.
        """
        before_h1 = data.get("heading_noise", {}).get("headings_before_h1", [])
        quality = data.get("heading_quality", {})
        empty_headings = quality.get("empty_headings", [])
        level_skips = quality.get("level_skips", [])

        problems: list[str] = []
        details: list[str] = []

        if before_h1:
            sample = ", ".join(f"{h['tag'].upper()}: {h['text']}" for h in before_h1[:3])
            problems.append(f"{len(before_h1)} nagłówków H2/H3 przed głównym H1 (np. {sample})")
            details.extend(f"{h['tag'].upper()}: {h['text']}" for h in before_h1)
        if empty_headings:
            problems.append(f"{len(empty_headings)} pustych nagłówków ({', '.join(empty_headings[:5])})")
            details.extend(f"{tag}: (pusty nagłówek)" for tag in empty_headings)
        if level_skips:
            sample = ", ".join(f"{s['from']} -> {s['to']}" for s in level_skips[:3])
            problems.append(f"{len(level_skips)} przeskoków poziomów nagłówków ({sample})")
            details.extend(f"{s['from']} -> {s['to']}: {s['text']}" for s in level_skips)

        if not problems:
            status = "ok"
            note = "Hierarchia nagłówków jest poprawna: H1 przed sekcjami, bez pustych nagłówków i przeskoków poziomów."
        else:
            # Puste nagłówki i przeskoki poziomów są usterką struktury dokumentu, ale
            # nie blokują indeksacji - stąd ostrzeżenie, a nie błąd krytyczny.
            status = "warning"
            note = "Wykryto problemy w hierarchii nagłówków: " + "; ".join(problems) + "."

        value = {
            "headings_before_h1": before_h1,
            "empty_headings": empty_headings,
            "level_skips": level_skips,
            "note": note,
        }
        current_value = "; ".join(details) if details else "(hierarchia nagłówków bez zastrzeżeń)"
        return self._make_metric("structure", "heading_order", value, status, current_value=current_value)

    def _evaluate_heading_noise(self, data: dict) -> dict:
        noisy = data.get("heading_noise", {}).get("noisy_headings", [])
        if noisy:
            sample = ", ".join(f"{h['tag'].upper()}: {h['text']}" for h in noisy[:3])
            status, note = "warning", f"Wykryto {len(noisy)} nagłówków H3/H4 o charakterze nawigacyjnym (np. {sample})."
        else:
            status, note = "ok", "Nie wykryto nagłówków H3/H4 o charakterze szumu nawigacyjnego."
        value = {"noisy_headings": noisy, "note": note}
        current_value = (
            "; ".join(f"{h['tag'].upper()}: {h['text']}" for h in noisy)
            if noisy
            else "(brak nagłówków o charakterze nawigacyjnym)"
        )
        return self._make_metric("structure", "heading_noise", value, status, current_value=current_value)

    def _evaluate_image_quality(self, data: dict) -> dict:
        total = data.get("images_total", 0)
        without_title = data.get("images_without_title", 0)
        non_ascii_count = data.get("images_non_ascii_src_count", 0)
        examples = data.get("images_non_ascii_src_examples", [])

        issues = []
        if without_title and total:
            issues.append(f"{without_title}/{total} obrazków bez atrybutu title")
        if non_ascii_count:
            issues.append(f"{non_ascii_count} adresów src ze znakami spoza ASCII")

        if total == 0:
            status, note = "ok", "Brak obrazków na stronie."
        elif not issues:
            status, note = "ok", "Pliki graficzne mają poprawne atrybuty i nazwy (ASCII)."
        else:
            status, note = "warning", "; ".join(issues) + "."

        value = {
            "without_title": without_title,
            "non_ascii_src_count": non_ascii_count,
            "non_ascii_src_examples": examples,
            "note": note,
        }
        current_value = "; ".join(examples) if examples else "(brak problematycznych plików graficznych)"
        return self._make_metric("structure", "image_quality", value, status, current_value=current_value)

    def _evaluate_eeat_authorship(self, data: dict) -> dict:
        """EEAT+ jest kontekstowy wg typu podstrony: brak sygnału autorstwa na
        stronach głównych/ofertowych/usługowych (wszystko poza `article`) NIE jest
        błędem ani ostrzeżeniem - to opcjonalny element, wymagany przede wszystkim
        dla treści blogowych/poradnikowych (YMYL), stąd status INFO zamiast WARNING."""
        eeat = data.get("eeat", {})
        page_type = data.get("page_type", "generic")
        has_signal = eeat.get("has_author_signal", False)

        if has_signal:
            status, note = "ok", 'Wykryto sygnał autorstwa treści (rel="author" / oznaczenie autora).'
        elif page_type not in EEAT_REQUIRED_PAGE_TYPES:
            status, note = "info", "Element wymagany głównie dla artykułów blogowych i treści wiedzy (YMYL)."
        else:
            status, note = "warning", "Brak wyraźnego sygnału autorstwa treści (E-E-A-T)."

        value = {"has_author_signal": has_signal, "page_type": page_type, "note": note}
        current_value = (
            "Wykryto oznaczenie autora treści na stronie." if has_signal else "(brak oznaczenia autora treści)"
        )
        return self._make_metric("structure", "eeat_authorship", value, status, current_value=current_value)

    def _evaluate_eeat_freshness(self, data: dict) -> dict:
        """Analogicznie do autorstwa: brak znacznika aktualizacji treści (Live Update
        Badge) poza artykułami jest statusem INFO, nie WARNING - patrz
        `_evaluate_eeat_authorship`."""
        eeat = data.get("eeat", {})
        page_type = data.get("page_type", "generic")
        modified_time = eeat.get("modified_time")

        if modified_time:
            status, note = "ok", f"Wykryto znacznik aktualizacji treści (article:modified_time: {modified_time})."
        elif page_type not in EEAT_REQUIRED_PAGE_TYPES:
            status, note = "info", "Element wymagany głównie dla artykułów blogowych i treści wiedzy (YMYL)."
        else:
            status, note = "warning", "Brak znacznika article:modified_time - trudno ocenić aktualność treści."

        value = {"modified_time": modified_time, "page_type": page_type, "note": note}
        current_value = modified_time if modified_time else "(brak znacznika article:modified_time)"
        return self._make_metric("structure", "eeat_freshness", value, status, current_value=current_value)

    def _evaluate_meta_keywords(self, data: dict) -> dict:
        present = data.get("meta_keywords_present", False)
        if present:
            status, note = "warning", (
                'Wykryto zbędny znacznik <meta name="keywords"> - Google i inne wyszukiwarki '
                "od dawna go ignorują, a jego obecność może ujawniać konkurencji strategię słów kluczowych."
            )
        else:
            status, note = "ok", "Brak zbędnego znacznika Meta Keywords."
        current_value = 'Wykryto znacznik <meta name="keywords">.' if present else "(brak znacznika Meta Keywords)"
        return self._make_metric(
            "seo", "meta_keywords", {"present": present, "note": note}, status, current_value=current_value
        )

    def _evaluate_internal_linking(self, data: dict) -> dict:
        count = data.get("internal_links_count", 0)
        if count == 0:
            status, note = "error", (
                "Brak jakichkolwiek linków wewnętrznych - utrudnia to robotom wyszukiwarek "
                "odkrywanie pozostałych podstron serwisu."
            )
        elif count < INTERNAL_LINKING_MIN:
            status, note = "warning", (
                f"Wykryto tylko {count} link(i) wewnętrzne - zalecane jest rozbudowanie linkowania "
                "między powiązanymi tematycznie podstronami."
            )
        else:
            status, note = "ok", f"Wykryto {count} linków wewnętrznych - architektura nawigacji wygląda prawidłowo."
        current_value = f"{count} linków wewnętrznych na stronie"
        return self._make_metric(
            "technical", "internal_linking", {"count": count, "note": note}, status, current_value=current_value
        )

    def _evaluate_js_rendering(self, data: dict) -> dict:
        js = data.get("js_rendering", {})
        likely_csr = js.get("likely_csr", False)
        word_count = js.get("word_count", 0)
        script_count = js.get("script_count", 0)

        if likely_csr:
            status, note = "warning", (
                f"Widoczny tekst strony jest bardzo krótki ({word_count} słów) przy dużej liczbie "
                f"skryptów ({script_count}) - treść może być renderowana wyłącznie po stronie klienta "
                "(CSR), niewidoczna dla części robotów wyszukiwarek i modeli LLM, które nie wykonują JavaScript."
            )
        else:
            status, note = "ok", (
                f"Strona zawiera wystarczającą ilość widocznego tekstu ({word_count} słów) dostępnego "
                "bez wykonywania JavaScript (SSR/statyczny HTML)."
            )
        current_value = f"{word_count} słów widocznego tekstu, {script_count} znaczników <script>"
        return self._make_metric(
            "technical", "javascript_rendering", {**js, "note": note}, status, current_value=current_value
        )

    def _evaluate_redirects(self, data: dict) -> dict:
        count = data.get("redirect_count", 0)
        if count == 0:
            status, note = "ok", "Adres audytowanej strony nie wymagał żadnego przekierowania."
        elif count == 1:
            status, note = "ok", (
                "Wykryto jedno przekierowanie do finalnego adresu - typowa sytuacja "
                "(np. http→https albo z/bez www)."
            )
        else:
            status, note = "warning", (
                f"Wykryto łańcuch {count} przekierowań (301/302) - zbyt długie łańcuchy spowalniają "
                "indeksację oraz ładowanie strony."
            )
        current_value = f"{count} przekierowań w łańcuchu do finalnego adresu"
        return self._make_metric(
            "technical", "redirect_chain", {"count": count, "note": note}, status, current_value=current_value
        )

    # ------------------------------------------------------------------
    # Dodatkowe, opcjonalne sprawdzenia sieciowe (robots.txt, dedykowana strona 404,
    # waga plików graficznych) - każde wywoływane i zabezpieczane niezależnie, żeby
    # błąd jednego z nich nigdy nie przerwał audytu ani nie wpłynął na pozostałe.
    # ------------------------------------------------------------------
    def _build_extra_checks_metrics(self, url: str, data: dict) -> list[dict]:
        try:
            robots = self.scraper.check_robots_txt(url)
        except Exception:
            logger.exception("Błąd podczas sprawdzania robots.txt dla %s.", url)
            robots = {"checked": False, "exists": False, "disallows_all": False, "blocked_ai_bots": []}

        try:
            http_errors = self.scraper.check_custom_404_page(url)
        except Exception:
            logger.exception("Błąd podczas sprawdzania dedykowanej strony 404 dla %s.", url)
            http_errors = {"checked": False, "returns_404": False, "status_code": None}

        try:
            image_sizes = self.scraper.check_image_sizes(data.get("images_checkable_srcs", []))
        except Exception:
            logger.exception("Błąd podczas sprawdzania wagi obrazków dla %s.", url)
            image_sizes = {"checked_count": 0, "oversized": []}

        try:
            wayback = self.wayback_service.fetch_domain_history(url)
        except Exception:
            logger.exception("Błąd podczas sprawdzania historii domeny w Wayback Machine dla %s.", url)
            wayback = {"available": False, "archived": False, "first_snapshot": None,
                       "age_years": None, "age_days": None, "snapshot_url": None,
                       "error": "Nie udało się odpytać archiwum."}

        return [
            self._evaluate_robots_txt(robots),
            self._evaluate_robots_ai_bots(robots, data),
            # Liczona TUTAJ, a nie w `_build_metrics`, bo potrzebuje kontekstu robots.txt -
            # oba testy mówią o indeksacji i nie mogą sobie przeczyć.
            self._evaluate_meta_robots(data, robots),
            self._evaluate_http_errors(http_errors),
            self._evaluate_image_compression(image_sizes),
            self._evaluate_wayback_domain_age(wayback),
        ]

    def _evaluate_meta_robots(self, data: dict, robots: dict) -> dict:
        """Dyrektywy `<meta name="robots">` sterujące indeksacją tej konkretnej podstrony.

        Test świadomie uwzględnia stan `robots.txt`: jeśli plik blokuje całą witrynę,
        komunikat nie może twierdzić, że strona "jest dostępna do indeksowania" - to
        byłaby sprzeczność z testem `robots_txt`, który zgłasza wtedy błąd krytyczny.
        Dwa różne mechanizmy opisują ten sam skutek, więc raportują go spójnie.
        """
        meta_robots = data.get("meta_robots", {})
        x_robots = data.get("x_robots_tag", {})
        # Nagłówek X-Robots-Tag jest RÓWNOWAŻNY znacznikowi meta - jedna dyrektywa
        # wystarczy, żeby strona wypadła z indeksu, więc oceniamy oba źródła łącznie.
        noindex = meta_robots.get("noindex", False) or x_robots.get("noindex", False)
        nofollow = meta_robots.get("nofollow", False) or x_robots.get("nofollow", False)
        directives = sorted(set(meta_robots.get("directives", [])) | set(x_robots.get("directives", [])))
        site_blocked = bool(robots.get("exists") and robots.get("disallows_all"))
        source = "nagłówek HTTP X-Robots-Tag" if x_robots.get("noindex") else 'znacznik <meta name="robots">'

        zrodla = list(meta_robots.get("raw", []))
        if x_robots.get("raw"):
            zrodla.append(f"X-Robots-Tag: {x_robots['raw']}")
        current_value = "; ".join(zrodla) or '(brak dyrektyw robots w meta i nagłówkach HTTP)'

        if noindex:
            status = "error"
            note = (
                f'Dyrektywa "noindex" ({source}) - ta podstrona jest celowo wykluczona '
                "z indeksu wyszukiwarki i nie pojawi się w wynikach."
            )
        elif nofollow:
            status = "warning"
            note = (
                'Dyrektywa "nofollow" - wyszukiwarka nie podąży za linkami z tej strony, '
                "co ogranicza przepływ mocy do podstron."
            )
        elif site_blocked:
            # Bez tego warunku test mówiłby "strona jest dostępna do indeksowania" w audycie,
            # w którym robots.txt blokuje cały serwis - dwa wykluczające się komunikaty.
            status = "warning"
            note = (
                "Znaczniki meta nie blokują indeksacji tej podstrony, ale plik robots.txt "
                "blokuje całą witrynę - to blokada nadrzędna (patrz test pliku robots.txt)."
            )
        elif directives:
            status = "ok"
            note = f"Dyrektywy robots nie blokują indeksacji ({', '.join(directives)})."
        else:
            status = "ok"
            note = "Brak dyrektyw robots w meta i nagłówkach HTTP - domyślnie strona jest indeksowana."

        return self._make_metric(
            "technical",
            "meta_robots",
            {
                "present": meta_robots.get("present", False),
                "directives": directives,
                "noindex": noindex,
                "nofollow": nofollow,
                "blocked_by_robots_txt": site_blocked,
                "x_robots_tag_present": x_robots.get("present", False),
                "x_robots_tag_raw": x_robots.get("raw", ""),
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_wayback_domain_age(self, wayback: dict) -> dict:
        """Wiek domeny oszacowany na podstawie pierwszej migawki w Internet Archive.

        Archiwum nie jest rejestrem domen - data pierwszej migawki mówi tylko, od kiedy
        Internet Archive zna ten adres, więc wynik jest oszacowaniem "od dołu" (domena
        może być starsza niż wskazuje archiwum). Dlatego młoda domena to INFO/WARNING
        wymagające weryfikacji, a nigdy błąd krytyczny - brak w archiwum nie jest usterką
        strony, tylko kontekstem do budowania autorytetu.
        """
        if not wayback.get("available"):
            return self._make_metric(
                "technical",
                "wayback_domain_age",
                {
                    "archived": False,
                    "first_snapshot": None,
                    "age_years": None,
                    "note": "Nie udało się sprawdzić historii domeny w archiwum Internet Archive.",
                },
                "info",
                current_value="(archiwum Internet Archive niedostępne podczas audytu)",
                generate_recommendation=False,
            )

        if not wayback.get("archived"):
            note = (
                "Domena nie występuje w archiwum Internet Archive - to typowe dla adresów "
                "zarejestrowanych niedawno. Nowa domena startuje bez historii i zaufania, "
                "więc na efekty SEO trzeba poczekać dłużej."
            )
            return self._make_metric(
                "technical",
                "wayback_domain_age",
                {"archived": False, "first_snapshot": None, "age_years": None, "note": note},
                "warning",
                current_value="(brak jakiejkolwiek migawki w Wayback Machine)",
            )

        age_years = wayback.get("age_years") or 0
        first_snapshot = wayback.get("first_snapshot")

        if age_years >= DOMAIN_AGE_ESTABLISHED_YEARS:
            status = "ok"
            note = (
                f"Domena ma ugruntowaną historię - pierwsza migawka w archiwum pochodzi z "
                f"{first_snapshot} (ok. {age_years} lat temu)."
            )
        elif age_years >= DOMAIN_AGE_YOUNG_YEARS:
            status = "info"
            note = (
                f"Domena jest stosunkowo młoda - pierwsza migawka z {first_snapshot} "
                f"(ok. {age_years} lat temu). Historia dopiero się buduje."
            )
        else:
            status = "warning"
            note = (
                f"Bardzo młoda domena - pierwsza migawka w archiwum pochodzi dopiero z "
                f"{first_snapshot} (ok. {age_years} lat temu). Warto zaplanować budowę "
                "autorytetu: regularne publikacje i wartościowe linki zewnętrzne."
            )

        current_value = f"Pierwsza migawka w Wayback Machine: {first_snapshot} (szacowany wiek: {age_years} lat)"
        if wayback.get("snapshot_url"):
            current_value += "\nAdres migawki: " + wayback["snapshot_url"]

        return self._make_metric(
            "technical",
            "wayback_domain_age",
            {
                "archived": True,
                "first_snapshot": first_snapshot,
                "age_years": age_years,
                "age_days": wayback.get("age_days"),
                "snapshot_url": wayback.get("snapshot_url"),
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_robots_ai_bots(self, robots: dict, data: dict | None = None) -> dict:
        """AI & GEO: czy witryna nie jest odcięta od modeli językowych.

        Sprawdzane są DWA mechanizmy blokowania, bo wystarczy jeden, żeby wykluczyć
        treść z odpowiedzi AI:
          * `robots.txt` (Disallow dla user-agenta bota),
          * nagłówek HTTP `X-Robots-Tag` z nazwą bota (np. "GPTBot: noindex").

        Zablokowanie GPTBot/ClaudeBot/PerplexityBot/Google-Extended/Bytespider wyklucza
        treść z odpowiedzi generowanych przez AI. To świadoma decyzja biznesowa u części
        wydawców (ochrona treści przed trenowaniem modeli), dlatego zgłaszamy to jako
        OSTRZEŻENIE do weryfikacji, a nie błąd krytyczny.
        """
        x_robots = (data or {}).get("x_robots_tag", {})
        header_blocked = [
            bot for bot in AI_BOT_USER_AGENTS
            if any(bot.lower() == blocked.lower() for blocked in x_robots.get("blocked_bots", []))
        ]
        # Globalny "noindex" w nagłówku dotyczy wszystkich crawlerów, także AI.
        if x_robots.get("noindex"):
            header_blocked = list(AI_BOT_USER_AGENTS)
        if (not robots.get("checked") or not robots.get("exists")) and not header_blocked:
            return self._make_metric(
                "technical",
                "robots_ai_bots",
                {
                    "blocked_bots": [],
                    "blocked_in_robots_txt": [],
                    "blocked_in_http_header": [],
                    "checked_bots": list(AI_BOT_USER_AGENTS),
                    "note": "Brak pliku robots.txt - boty AI nie są blokowane (mają pełny dostęp).",
                },
                "ok",
                current_value="(brak pliku robots.txt - domyślnie pełny dostęp dla botów AI)",
                generate_recommendation=False,
            )

        # Zbiór z obu mechanizmów, w kolejności zgodnej z AI_BOT_USER_AGENTS.
        blocked_set = set(robots.get("blocked_ai_bots") or []) | set(header_blocked)
        blocked = [bot for bot in AI_BOT_USER_AGENTS if bot in blocked_set]
        if blocked:
            status = "warning"
            note = (
                f"Plik robots.txt blokuje {len(blocked)} bot(ów) AI: {', '.join(blocked)} - "
                "treść tej witryny nie trafi do odpowiedzi generowanych przez modele językowe."
            )
            zrodla = [f"User-agent: {bot}\nDisallow: /" for bot in (robots.get("blocked_ai_bots") or [])]
            if header_blocked:
                zrodla.append(f"X-Robots-Tag: {x_robots.get('raw', 'noindex')}")
            current_value = "\n".join(zrodla)
        else:
            status = "ok"
            note = (
                f"Boty AI ({', '.join(AI_BOT_USER_AGENTS)}) mają dostęp do witryny - "
                "treść może być cytowana w odpowiedziach generowanych przez AI."
            )
            current_value = "(żaden z botów AI nie jest zablokowany w robots.txt)"

        return self._make_metric(
            "technical",
            "robots_ai_bots",
            {
                "blocked_bots": blocked,
                "blocked_in_robots_txt": robots.get("blocked_ai_bots") or [],
                "blocked_in_http_header": header_blocked,
                "checked_bots": list(AI_BOT_USER_AGENTS),
                "note": note,
            },
            status,
            current_value=current_value,
        )

    def _evaluate_robots_txt(self, robots: dict) -> dict:
        if not robots.get("exists"):
            status, note = "warning", "Nie znaleziono pliku robots.txt pod adresem /robots.txt."
            current_value = "(brak pliku robots.txt)"
        elif robots.get("disallows_all"):
            status, note = "error", (
                'Plik robots.txt blokuje indeksację CAŁEJ witryny dla wszystkich robotów '
                '("User-agent: *" + "Disallow: /").'
            )
            current_value = "User-agent: *\nDisallow: /"
        else:
            status, note = "ok", "Plik robots.txt istnieje i nie blokuje całej witryny."
            current_value = "Plik robots.txt jest dostępny pod /robots.txt."
        return self._make_metric(
            "technical", "robots_txt", {**robots, "note": note}, status, current_value=current_value
        )

    def _evaluate_http_errors(self, http_errors: dict) -> dict:
        status_code = http_errors.get("status_code")
        if not http_errors.get("checked"):
            status, note = "warning", "Nie udało się zweryfikować obsługi błędów 404 (błąd połączenia)."
            current_value = "(nie udało się sprawdzić)"
        elif http_errors.get("returns_404"):
            status, note = "ok", "Serwer poprawnie zwraca kod HTTP 404 dla nieistniejących adresów."
            current_value = f"Status HTTP dla nieistniejącego adresu: {status_code}"
        else:
            status, note = "warning", (
                f'Nieistniejący adres zwrócił status {status_code} zamiast 404 (tzw. "miękkie 404") - '
                "może to dezorientować roboty wyszukiwarek co do tego, które adresy naprawdę istnieją."
            )
            current_value = f"Status HTTP dla nieistniejącego adresu: {status_code}"
        return self._make_metric(
            "technical", "http_errors", {**http_errors, "note": note}, status, current_value=current_value
        )

    def _evaluate_image_compression(self, image_sizes: dict) -> dict:
        oversized = image_sizes.get("oversized", [])
        checked_count = image_sizes.get("checked_count", 0)

        if checked_count == 0:
            status, note = "ok", "Nie znaleziono obrazków możliwych do zweryfikowania (lub serwer nie zwrócił wagi plików)."
            current_value = "(brak danych o wadze plików graficznych)"
        elif oversized:
            examples = ", ".join(f"{item['src'].rsplit('/', 1)[-1]} ({item['size_kb']} KB)" for item in oversized[:3])
            status, note = "warning", (
                f"{len(oversized)}/{checked_count} sprawdzonych obrazków przekracza 100 KB "
                f"(np. {examples}) - warto je skompresować lub przekonwertować do formatu WebP/AVIF."
            )
            current_value = "; ".join(f"{item['src']} — {item['size_kb']} KB" for item in oversized)
        else:
            status, note = "ok", f"Wszystkie sprawdzone obrazki ({checked_count}) mieszczą się w limicie 100 KB."
            current_value = f"Sprawdzono {checked_count} obrazków - wszystkie poniżej 100 KB."

        return self._make_metric(
            "performance", "image_compression", {**image_sizes, "note": note}, status, current_value=current_value
        )

    # ------------------------------------------------------------------
    # Google PageSpeed Insights -> metryki wydajności / Core Web Vitals
    # (równolegle dla strategii mobile i desktop, metryki z przedrostkiem)
    # ------------------------------------------------------------------
    def _build_pagespeed_metrics(self, url: str) -> list[dict]:
        results = self.pagespeed_service.analyze_all(url)
        metrics = [self._build_pagespeed_summary_metric(results)]
        for strategy in ("mobile", "desktop"):
            metrics.extend(self._build_pagespeed_metrics_for_strategy(strategy, results[strategy]))
        return metrics

    def _build_pagespeed_summary_metric(self, results: dict) -> dict:
        """Łączy ogólne wyniki punktowe PageSpeed (Mobile + Desktop) w jedną metrykę,
        żeby w podsumowaniu (Priorytety/Ostrzeżenia) nie powielać dwóch osobnych kart
        i wygenerować przez RAGEngine tylko jedną, kompleksową poradę."""
        mobile_score = results["mobile"].get("performance_score")
        desktop_score = results["desktop"].get("performance_score")

        mobile_status = self._pagespeed_score_status(mobile_score)
        desktop_status = self._pagespeed_score_status(desktop_score)
        status = self._worse_status(mobile_status, desktop_status)

        mobile_display = f"{mobile_score}/100" if mobile_score is not None else "brak danych"
        desktop_display = f"{desktop_score}/100" if desktop_score is not None else "brak danych"
        label = f"Wynik PageSpeed Insights (Mobile: {mobile_display}, Desktop: {desktop_display})"

        if mobile_score is None and desktop_score is None:
            note = "Nie udało się pobrać wyniku wydajności PageSpeed Insights ani dla Mobile, ani dla Desktop."
        elif mobile_score is None or desktop_score is None:
            missing = "Mobile" if mobile_score is None else "Desktop"
            note = f"Nie udało się pobrać wyniku PageSpeed Insights dla wariantu {missing}."
        elif status == "ok":
            note = "Wydajność strony na urządzeniach mobilnych i desktopowych mieści się w dobrych progach Google PageSpeed."
        else:
            weaker = "mobilnych" if mobile_score <= desktop_score else "desktopowych"
            note = (
                f"Wydajność jest niższa na urządzeniach {weaker}. Google ocenia witryny przede wszystkim na "
                "podstawie wersji mobilnej (mobile-first indexing), dlatego wynik mobilny ma priorytet "
                "przy optymalizacji."
            )

        value = {
            "label": label,
            "mobile_score": mobile_score,
            "desktop_score": desktop_score,
            "note": note,
        }
        return self._make_metric("performance", "pagespeed_score", value, status)

    def _pagespeed_score_status(self, score: int | None) -> str:
        if score is None:
            return "warning"
        if score >= PAGESPEED_SCORE_GOOD:
            return "ok"
        if score >= PAGESPEED_SCORE_WARNING:
            return "warning"
        return "error"

    def _worse_status(self, a: str, b: str) -> str:
        severity = {"ok": 0, "warning": 1, "error": 2}
        return a if severity[a] >= severity[b] else b

    def _build_pagespeed_metrics_for_strategy(self, strategy: str, result: dict) -> list[dict]:
        prefix = f"{strategy}_"

        if not result.get("available"):
            return [
                self._make_metric(
                    "performance",
                    f"{prefix}pagespeed",
                    {
                        "note": result.get("error") or "PageSpeed Insights niedostępne.",
                        "strategy": strategy,
                    },
                    "warning",
                )
            ]

        return [
            self._evaluate_pagespeed_score(result, prefix),
            self._evaluate_lcp(result, prefix),
            self._evaluate_cls(result, prefix),
            self._evaluate_fcp(result, prefix),
            self._evaluate_inp(result, prefix),
        ]

    def _evaluate_pagespeed_score(self, result: dict, prefix: str = "") -> dict:
        # Uwaga: rekomendacja RAG dla wyniku ogólnego generowana jest raz, zbiorczo,
        # w _build_pagespeed_summary_metric() - tutaj tylko dane do kafelka w zakładce.
        score = result["performance_score"]
        if score is None:
            status, note = "warning", "Brak wyniku wydajności PageSpeed."
        elif score >= PAGESPEED_SCORE_GOOD:
            status, note = "ok", f"Wynik wydajności PageSpeed: {score}/100."
        elif score >= PAGESPEED_SCORE_WARNING:
            status, note = "warning", f"Wynik wydajności PageSpeed: {score}/100 - warto poprawić."
        else:
            status, note = "error", f"Wynik wydajności PageSpeed: {score}/100 - niska wydajność."
        value = {"value": score, "unit": "", "label": "Wynik PageSpeed", "note": note, "strategy": prefix.rstrip("_")}
        return self._make_metric(
            "performance", f"{prefix}pagespeed_score", value, status, generate_recommendation=False
        )

    def _evaluate_lcp(self, result: dict, prefix: str = "") -> dict:
        lcp = result["lcp"]
        if lcp is None:
            status, note = "warning", "Brak danych LCP."
        elif lcp <= LCP_GOOD:
            status, note = "ok", f"LCP: {lcp:.2f}s (dobry wynik)."
        elif lcp <= LCP_WARNING:
            status, note = "warning", f"LCP: {lcp:.2f}s (wymaga poprawy)."
        else:
            status, note = "error", f"LCP: {lcp:.2f}s (słaby wynik)."
        value = {
            "value": round(lcp, 2) if lcp is not None else None,
            "unit": "s",
            "label": "LCP",
            "note": note,
            "strategy": prefix.rstrip("_"),
        }
        return self._make_metric("performance", f"{prefix}lcp", value, status)

    def _evaluate_cls(self, result: dict, prefix: str = "") -> dict:
        cls = result["cls"]
        if cls is None:
            status, note = "warning", "Brak danych CLS."
        elif cls <= CLS_GOOD:
            status, note = "ok", f"CLS: {cls:.3f} (dobry wynik)."
        elif cls <= CLS_WARNING:
            status, note = "warning", f"CLS: {cls:.3f} (wymaga poprawy)."
        else:
            status, note = "error", f"CLS: {cls:.3f} (słaby wynik)."
        value = {
            "value": round(cls, 3) if cls is not None else None,
            "unit": "",
            "label": "CLS",
            "note": note,
            "strategy": prefix.rstrip("_"),
        }
        return self._make_metric("performance", f"{prefix}cls", value, status)

    def _evaluate_fcp(self, result: dict, prefix: str = "") -> dict:
        fcp = result["fcp"]
        if fcp is None:
            status, note = "warning", "Brak danych FCP."
        elif fcp <= FCP_GOOD:
            status, note = "ok", f"FCP: {fcp:.2f}s (dobry wynik)."
        elif fcp <= FCP_WARNING:
            status, note = "warning", f"FCP: {fcp:.2f}s (wymaga poprawy)."
        else:
            status, note = "error", f"FCP: {fcp:.2f}s (słaby wynik)."
        value = {
            "value": round(fcp, 2) if fcp is not None else None,
            "unit": "s",
            "label": "FCP",
            "note": note,
            "strategy": prefix.rstrip("_"),
        }
        return self._make_metric("performance", f"{prefix}fcp", value, status)

    def _evaluate_inp(self, result: dict, prefix: str = "") -> dict:
        inp = result["inp"]
        if inp is None:
            status, note = "warning", "Brak danych INP (za mało danych z Chrome UX Report)."
        elif inp <= INP_GOOD:
            status, note = "ok", f"INP: {inp:.0f}ms (dobry wynik)."
        elif inp <= INP_WARNING:
            status, note = "warning", f"INP: {inp:.0f}ms (wymaga poprawy)."
        else:
            status, note = "error", f"INP: {inp:.0f}ms (słaby wynik)."
        value = {
            "value": round(inp) if inp is not None else None,
            "unit": "ms",
            "label": "INP",
            "note": note,
            "strategy": prefix.rstrip("_"),
        }
        return self._make_metric("performance", f"{prefix}inp", value, status)

    def _make_metric(
        self,
        category: str,
        key: str,
        value: dict,
        status: str,
        current_value: str | None = None,
        generate_recommendation: bool = True,
    ) -> dict:
        if generate_recommendation and status in ("warning", "error"):
            # `metric_key` steruje doborem modelu w RAGEngine (patrz COMPLEX_METRICS):
            # diagnoza LCP czy renderowania JS dostaje model mocniejszy, brak atrybutu
            # alt - tańszy. Bez przekazania klucza routing nigdy by się nie uruchomił.
            recommendation = self.rag_engine.generate_recommendation(
                value.get("note", key),
                category=category,
                current_value=current_value,
                metric_key=key,
            )
            # Pusty wynik zwraca _NullRecommendationEngine przy skanie podstron - nie ma
            # sensu zapisywać pustego klucza "recommendation" w metryce.
            if recommendation:
                value = {**value, "recommendation": recommendation}
        return {
            "category": category,
            "key": key,
            "value": value,
            "status": status,
            "current_value": current_value or "",
        }

    def _calculate_score(self, metrics: list[dict]) -> int:
        if not metrics:
            return 0
        total = sum(SCORE_WEIGHTS[m["status"]] for m in metrics)
        return round(total / len(metrics))
