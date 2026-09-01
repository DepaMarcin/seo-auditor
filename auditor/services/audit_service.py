from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .ga4_service import GA4OAuthService
from .pagespeed import PageSpeedService
from .rag import RAGEngine
from .scraper import ScraperError, SEOScraper
from .senuto import SenutoService

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

SCORE_WEIGHTS = {"ok": 100, "warning": 50, "error": 0}


class AuditService:
    """Orkiestrator audytu SEO: SEOScraper + PageSpeedService -> analiza metryk -> RAGEngine -> zapis do bazy."""

    def __init__(
        self,
        scraper: SEOScraper | None = None,
        rag_engine: RAGEngine | None = None,
        pagespeed_service: PageSpeedService | None = None,
        senuto_service: SenutoService | None = None,
        ga4_service: GA4OAuthService | None = None,
    ):
        self.scraper = scraper or SEOScraper()
        self.rag_engine = rag_engine or RAGEngine()
        self.pagespeed_service = pagespeed_service or PageSpeedService()
        self.senuto_service = senuto_service or SenutoService()
        self.ga4_service = ga4_service or GA4OAuthService()

    def run_audit(self, audit):
        from auditor.models import Audit

        audit.status = Audit.Status.PROCESSING
        audit.save(update_fields=["status"])

        try:
            data = self.scraper.scrape(audit.url)
        except ScraperError:
            logger.warning("Audyt %s nie powiódł się.", audit.pk, exc_info=True)
            audit.status = Audit.Status.FAILED
            audit.save(update_fields=["status"])
            return audit

        metrics = self._build_metrics(data)
        metrics.extend(self._build_pagespeed_metrics(audit.url))

        audit.metrics.all().delete()
        for metric in metrics:
            audit.metrics.create(**metric)

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
        audit.save(update_fields=["ga4_property_id", "ga4_organic_sessions", "ga4_history"])
        return audit

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
            self._evaluate_heading_order(data),
            self._evaluate_heading_noise(data),
            self._evaluate_image_quality(data),
            self._evaluate_eeat_authorship(data),
            self._evaluate_eeat_freshness(data),
        ]

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
        before_h1 = data.get("heading_noise", {}).get("headings_before_h1", [])
        if before_h1:
            sample = ", ".join(f"{h['tag'].upper()}: {h['text']}" for h in before_h1[:3])
            status, note = "warning", f"Wykryto {len(before_h1)} nagłówków H2/H3 przed głównym H1 (np. {sample})."
        else:
            status, note = "ok", "Nagłówek H1 pojawia się przed innymi nagłówkami sekcji."
        value = {"headings_before_h1": before_h1, "note": note}
        current_value = (
            "; ".join(f"{h['tag'].upper()}: {h['text']}" for h in before_h1)
            if before_h1
            else "(H1 jest pierwszym nagłówkiem na stronie)"
        )
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
        eeat = data.get("eeat", {})
        if eeat.get("has_author_signal"):
            status, note = "ok", 'Wykryto sygnał autorstwa treści (rel="author" / oznaczenie autora).'
        else:
            status, note = "warning", "Brak wyraźnego sygnału autorstwa treści (E-E-A-T)."
        value = {"has_author_signal": eeat.get("has_author_signal", False), "note": note}
        current_value = (
            "Wykryto oznaczenie autora treści na stronie."
            if eeat.get("has_author_signal")
            else "(brak oznaczenia autora treści)"
        )
        return self._make_metric("structure", "eeat_authorship", value, status, current_value=current_value)

    def _evaluate_eeat_freshness(self, data: dict) -> dict:
        eeat = data.get("eeat", {})
        modified_time = eeat.get("modified_time")
        if modified_time:
            status, note = "ok", f"Wykryto znacznik aktualizacji treści (article:modified_time: {modified_time})."
        else:
            status, note = "warning", "Brak znacznika article:modified_time - trudno ocenić aktualność treści."
        value = {"modified_time": modified_time, "note": note}
        current_value = modified_time if modified_time else "(brak znacznika article:modified_time)"
        return self._make_metric("structure", "eeat_freshness", value, status, current_value=current_value)

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
