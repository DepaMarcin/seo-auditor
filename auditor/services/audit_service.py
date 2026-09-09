from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from .ga4_insights import analyze_channel_trends
from .ga4_service import GA4OAuthService
from .gsc_insights import generate_page_commentary, generate_query_commentary
from .gsc_service import GSCService
from .pagespeed import PageSpeedService
from .rag import RAGEngine
from .scraper import AI_BOT_USER_AGENTS, ScraperError, SEOScraper
from .senuto import SenutoService
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

SCORE_WEIGHTS = {"ok": 100, "info": 100, "warning": 50, "error": 0}


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
        current_value = title if title else "(brak tagu <title>)"
        if not title:
            status, note = "error", "Brak tagu <title>."
        elif length < TITLE_MIN_LENGTH or length > TITLE_MAX_LENGTH:
            status, note = (
                "warning",
                f"Długość tytułu ({length} znaków) poza zalecanym zakresem "
                f"{TITLE_MIN_LENGTH}-{TITLE_MAX_LENGTH}.",
            )
        else:
            status, note = "ok", "Długość tytułu jest prawidłowa."
        return self._make_metric(
            "seo", "title", {"value": title, "length": length, "note": note}, status,
            current_value=current_value,
        )

    def _evaluate_description(self, data: dict) -> dict:
        description = data.get("meta_description")
        length = data.get("meta_description_length", 0)
        current_value = description if description else "(brak meta description)"
        if not description:
            status, note = "error", "Brak meta description."
        elif length < DESCRIPTION_MIN_LENGTH or length > DESCRIPTION_MAX_LENGTH:
            status, note = (
                "warning",
                f"Długość opisu ({length} znaków) poza zalecanym zakresem "
                f"{DESCRIPTION_MIN_LENGTH}-{DESCRIPTION_MAX_LENGTH}.",
            )
        else:
            status, note = "ok", "Długość meta description jest prawidłowa."
        return self._make_metric(
            "seo", "meta_description", {"value": description, "length": length, "note": note}, status,
            current_value=current_value,
        )

    def _evaluate_h1(self, data: dict) -> dict:
        h1_headings = data.get("headings", {}).get("h1", [])
        h1_count = data.get("h1_count", 0)
        current_value = "; ".join(h1_headings) if h1_headings else "(brak nagłówka H1)"
        if h1_count == 0:
            status, note = "error", "Brak nagłówka H1."
        elif h1_count > 1:
            status, note = "warning", f"Wykryto {h1_count} nagłówków H1 (zalecany dokładnie 1)."
        else:
            status, note = "ok", "Struktura H1 jest prawidłowa."
        return self._make_metric(
            "technical",
            "h1_structure",
            {"count": h1_count, "headings": h1_headings, "note": note},
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
            self._evaluate_robots_ai_bots(robots),
            self._evaluate_http_errors(http_errors),
            self._evaluate_image_compression(image_sizes),
            self._evaluate_wayback_domain_age(wayback),
        ]

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

    def _evaluate_robots_ai_bots(self, robots: dict) -> dict:
        """AI & GEO: czy robots.txt nie odcina witryny od modeli językowych.

        Zablokowanie GPTBot/ClaudeBot/PerplexityBot/Bytespider wyklucza treść z
        odpowiedzi generowanych przez AI. To świadoma decyzja biznesowa u części
        wydawców (ochrona treści przed trenowaniem modeli), dlatego zgłaszamy to jako
        OSTRZEŻENIE do weryfikacji, a nie błąd krytyczny.
        """
        if not robots.get("checked") or not robots.get("exists"):
            return self._make_metric(
                "technical",
                "robots_ai_bots",
                {
                    "blocked_bots": [],
                    "checked_bots": list(AI_BOT_USER_AGENTS),
                    "note": "Brak pliku robots.txt - boty AI nie są blokowane (mają pełny dostęp).",
                },
                "ok",
                current_value="(brak pliku robots.txt - domyślnie pełny dostęp dla botów AI)",
                generate_recommendation=False,
            )

        blocked = robots.get("blocked_ai_bots") or []
        if blocked:
            status = "warning"
            note = (
                f"Plik robots.txt blokuje {len(blocked)} bot(ów) AI: {', '.join(blocked)} - "
                "treść tej witryny nie trafi do odpowiedzi generowanych przez modele językowe."
            )
            current_value = "\n".join(f"User-agent: {bot}\nDisallow: /" for bot in blocked)
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
            {"blocked_bots": blocked, "checked_bots": list(AI_BOT_USER_AGENTS), "note": note},
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
            recommendation = self.rag_engine.generate_recommendation(
                value.get("note", key), category=category, current_value=current_value
            )
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
