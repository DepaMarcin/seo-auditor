"""Testy modułów: walidacja grafu Schema.org, E-E-A-T i GEO Suppression.

Scenariusze odwzorowują usterki z audytu FreshGift (rozjechany graf encji, cena B2B
w Schema, wyciek środowiska testowego, opinie ukryte przez CSS). Żaden test nie łączy
się z siecią.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from auditor.services.audit_service import AuditService
from auditor.services.scraper import SEOScraper


def _service() -> AuditService:
    rag = MagicMock()
    rag.generate_recommendation.return_value = "Rekomendacja testowa."
    return AuditService(rag_engine=rag)


def _page(schema: dict | list | None = None, body: str = "", url: str = "https://sklep.pl/produkt/x") -> dict:
    """Buduje stronę z opcjonalnym blokiem JSON-LD i zwraca wynik parsowania."""
    skrypt = (
        f'<script type="application/ld+json">{json.dumps(schema, ensure_ascii=False)}</script>'
        if schema is not None else ""
    )
    html = f"<html><head><title>Strona testowa</title>{skrypt}</head><body>{body}</body></html>"
    return SEOScraper().parse(html, url)


# ----------------------------------------------------------------------
# MODUŁ 1: walidacja grafu Schema.org
# ----------------------------------------------------------------------

class SchemaEntityLinkingTests(SimpleTestCase):
    def test_graph_entities_are_flattened(self):
        data = _page({"@context": "https://schema.org", "@graph": [
            {"@type": "Organization", "@id": "https://sklep.pl/#org"},
            {"@type": "WebSite", "@id": "https://sklep.pl/#site"},
        ]})

        typy = [e.get("@type") for e in data["schema"]["entities"]]
        self.assertIn("Organization", typy)
        self.assertIn("WebSite", typy)

    def test_nested_entities_are_also_collected(self):
        """Encje zagnieżdżone w właściwościach muszą być widoczne dla walidacji."""
        data = _page({"@type": "Product", "offers": {"@type": "Offer", "price": "10"}})

        typy = [e.get("@type") for e in data["schema"]["entities"]]
        self.assertIn("Offer", typy)

    def test_duplicate_organizations_are_a_critical_error(self):
        data = _page({"@graph": [
            {"@type": "Organization", "@id": "https://sklep.pl/#org", "name": "A"},
            {"@type": "Organization", "@id": "https://sklep.pl/#firma", "name": "A Sp. z o.o."},
        ]})

        metric = _service()._evaluate_schema_entity_linking(data)

        self.assertEqual(metric["status"], "error")
        self.assertTrue(metric["value"]["duplicates"])

    def test_nested_publisher_instead_of_reference_is_a_warning(self):
        data = _page({"@graph": [
            {"@type": "WebPage", "@id": "https://sklep.pl/#page",
             "publisher": {"@type": "Organization", "name": "Sklep"}},
        ]})

        metric = _service()._evaluate_schema_entity_linking(data)

        self.assertEqual(metric["status"], "warning")
        self.assertTrue(metric["value"]["missing_links"])

    def test_proper_references_are_ok(self):
        data = _page({"@graph": [
            {"@type": "Organization", "@id": "https://sklep.pl/#org"},
            {"@type": "WebSite", "@id": "https://sklep.pl/#site",
             "publisher": {"@id": "https://sklep.pl/#org"}},
            {"@type": "WebPage", "@id": "https://sklep.pl/#page",
             "isPartOf": {"@id": "https://sklep.pl/#site"}},
        ]})

        self.assertEqual(_service()._evaluate_schema_entity_linking(data)["status"], "ok")

    def test_nested_offer_in_product_is_not_flagged(self):
        """Offer i brand należą do produktu - wymaganie dla nich @id dałoby fałszywy alarm."""
        data = _page({"@type": "Product", "@id": "https://sklep.pl/#prod",
                      "brand": {"@type": "Brand", "name": "Marka"},
                      "offers": {"@type": "Offer", "price": "99"}})

        self.assertEqual(_service()._evaluate_schema_entity_linking(data)["status"], "ok")

    def test_missing_json_ld_is_an_error(self):
        data = _page(None)

        self.assertEqual(_service()._evaluate_schema_entity_linking(data)["status"], "error")


class PriceDiscrepancyTests(SimpleTestCase):
    BODY_129 = '<span class="price">129,00 zł</span>'

    def test_schema_price_lower_than_visible_is_critical(self):
        """Wzorzec FreshGift: do Schema trafiła cena hurtowa B2B, użytkownik widzi detaliczną."""
        data = _page({"@type": "Product", "offers": {"@type": "Offer", "price": "89.00"}},
                     body=self.BODY_129)

        metric = _service()._evaluate_price_discrepancy(data)

        self.assertEqual(metric["status"], "error")
        self.assertIn("NIŻSZA", metric["value"]["note"])

    def test_matching_price_is_ok(self):
        data = _page({"@type": "Product", "offers": {"@type": "Offer", "price": "129.00"}},
                     body=self.BODY_129)

        self.assertEqual(_service()._evaluate_price_discrepancy(data)["status"], "ok")

    def test_rounding_difference_is_tolerated(self):
        data = _page({"@type": "Product", "offers": {"@type": "Offer", "price": "128.00"}},
                     body=self.BODY_129)

        self.assertEqual(_service()._evaluate_price_discrepancy(data)["status"], "ok")

    def test_schema_price_higher_than_any_visible_is_a_warning(self):
        data = _page({"@type": "Product", "offers": {"@type": "Offer", "price": "299.00"}},
                     body=self.BODY_129)

        self.assertEqual(_service()._evaluate_price_discrepancy(data)["status"], "warning")

    def test_no_schema_price_is_info(self):
        data = _page({"@type": "Product", "name": "Produkt"}, body=self.BODY_129)

        self.assertEqual(_service()._evaluate_price_discrepancy(data)["status"], "info")

    def test_polish_and_english_number_formats_are_parsed(self):
        scraper = SEOScraper()

        self.assertEqual(scraper._parse_prices("1 234,56 zł"), [1234.56])
        self.assertEqual(scraper._parse_prices("$1,234.56"), [1234.56])

    def test_hidden_price_does_not_count_as_visible(self):
        data = _page(
            {"@type": "Product", "offers": {"@type": "Offer", "price": "89.00"}},
            body='<span class="price" style="display:none">89,00 zł</span>'
                 '<span class="price">129,00 zł</span>',
        )

        self.assertEqual(data["visible_prices"]["min_visible"], 129.0)
        self.assertEqual(_service()._evaluate_price_discrepancy(data)["status"], "error")


class SchemaDataHygieneTests(SimpleTestCase):
    def test_test_environment_leak_is_critical(self):
        data = _page({"@type": "Organization", "logo": "https://admin.freshgift.test/avatar.png"})

        metric = _service()._evaluate_schema_data_hygiene(data)

        self.assertEqual(metric["status"], "error")
        self.assertTrue(metric["value"]["env_leaks"])

    def test_localhost_in_sameas_is_detected(self):
        data = _page({"@type": "Organization", "sameAs": ["http://localhost:3000/profil"]})

        self.assertEqual(_service()._evaluate_schema_data_hygiene(data)["status"], "error")

    def test_double_encoded_entities_are_a_warning(self):
        data = _page({"@type": "Product", "description": "Owoce &amp;amp; słodycze"})

        metric = _service()._evaluate_schema_data_hygiene(data)

        self.assertEqual(metric["status"], "warning")
        self.assertTrue(metric["value"]["double_encoded"])

    def test_domain_suffix_in_product_name_is_a_warning(self):
        data = _page({"@type": "Product", "name": "Kosz prezentowy | FreshGift.pl"},
                     url="https://freshgift.pl/produkt/kosz")

        metric = _service()._evaluate_schema_data_hygiene(data)

        self.assertEqual(metric["status"], "warning")
        self.assertTrue(metric["value"]["seo_suffixes"])

    def test_clean_name_with_dash_is_not_flagged(self):
        """Myślnik w nazwie produktu sam w sobie nie jest usterką."""
        data = _page({"@type": "Product", "name": "Kosz prezentowy - duży"},
                     url="https://freshgift.pl/produkt/kosz")

        self.assertEqual(_service()._evaluate_schema_data_hygiene(data)["status"], "ok")


class EcommerceCompletenessTests(SimpleTestCase):
    KOMPLETNY = {
        "@type": "Product", "name": "Kosz",
        "brand": {"@type": "Brand", "name": "Marka"},
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.8"},
        "offers": {"@type": "Offer", "price": "129",
                   "shippingDetails": {"@type": "OfferShippingDetails"},
                   "hasMerchantReturnPolicy": {"@type": "MerchantReturnPolicy"}},
    }

    def test_complete_product_is_ok(self):
        self.assertEqual(
            _service()._evaluate_ecommerce_completeness(_page(self.KOMPLETNY))["status"], "ok"
        )

    def test_product_missing_everything_is_critical(self):
        data = _page({"@type": "Product", "name": "Kosz"})

        metric = _service()._evaluate_ecommerce_completeness(data)

        self.assertEqual(metric["status"], "error")
        self.assertGreaterEqual(len(metric["value"]["missing"]), 3)

    def test_empty_brand_is_reported(self):
        produkt = {**self.KOMPLETNY, "brand": {}}

        metric = _service()._evaluate_ecommerce_completeness(_page(produkt))

        self.assertTrue(any("brand" in m for m in metric["value"]["missing"]))

    def test_empty_collection_page_is_reported(self):
        data = _page({"@type": "CollectionPage", "@id": "https://sklep.pl/kat#page"})

        metric = _service()._evaluate_ecommerce_completeness(data)

        self.assertTrue(any("ItemList" in m for m in metric["value"]["missing"]))

    def test_non_ecommerce_page_is_info(self):
        data = _page({"@type": "WebPage"})

        self.assertEqual(_service()._evaluate_ecommerce_completeness(data)["status"], "info")


# ----------------------------------------------------------------------
# MODUŁ 2: E-E-A-T
# ----------------------------------------------------------------------

class AuthorshipDepthTests(SimpleTestCase):
    ARTYKUL_URL = "https://sklep.pl/blog/poradnik"

    def test_author_with_credentials_and_profile_is_ok(self):
        data = _page({"@type": "BlogPosting", "author": {
            "@type": "Person", "name": "Jan Kowalski", "jobTitle": "Ekspert SEO",
            "sameAs": ["https://linkedin.com/in/jan"]}}, url=self.ARTYKUL_URL)

        self.assertEqual(_service()._evaluate_authorship_depth(data)["status"], "ok")

    def test_bare_name_is_not_enough(self):
        data = _page({"@type": "BlogPosting", "author": {"@type": "Person", "name": "Jan Kowalski"}},
                     url=self.ARTYKUL_URL)

        metric = _service()._evaluate_authorship_depth(data)

        self.assertEqual(metric["status"], "warning")
        self.assertIn("sameAs", metric["value"]["missing"][0])

    def test_missing_sameas_alone_is_a_warning(self):
        data = _page({"@type": "BlogPosting", "author": {
            "@type": "Person", "name": "Jan", "jobTitle": "Ekspert"}}, url=self.ARTYKUL_URL)

        self.assertEqual(_service()._evaluate_authorship_depth(data)["status"], "warning")

    def test_article_without_person_is_a_warning(self):
        data = _page({"@type": "BlogPosting", "headline": "Poradnik"}, url=self.ARTYKUL_URL)

        self.assertEqual(_service()._evaluate_authorship_depth(data)["status"], "warning")

    def test_product_page_without_author_is_info_not_warning(self):
        """Wymóg autorstwa dotyczy treści poradnikowych, nie kart produktu."""
        data = _page({"@type": "Product", "name": "Kosz"}, url="https://sklep.pl/produkt/kosz")

        self.assertEqual(_service()._evaluate_authorship_depth(data)["status"], "info")


class FreshnessDecayTests(SimpleTestCase):
    def test_stale_content_without_update_is_a_warning(self):
        stara = (date.today() - timedelta(days=800)).isoformat()
        data = _page({"@type": "BlogPosting", "datePublished": stara, "dateModified": stara})

        metric = _service()._evaluate_freshness_decay(data)

        self.assertEqual(metric["status"], "warning")
        self.assertGreater(metric["value"]["age_days"], 365)

    def test_updated_content_is_ok(self):
        data = _page({"@type": "BlogPosting",
                      "datePublished": (date.today() - timedelta(days=800)).isoformat(),
                      "dateModified": (date.today() - timedelta(days=30)).isoformat()})

        self.assertEqual(_service()._evaluate_freshness_decay(data)["status"], "ok")

    def test_recent_content_without_update_is_ok(self):
        data = _page({"@type": "BlogPosting",
                      "datePublished": (date.today() - timedelta(days=30)).isoformat()})

        self.assertEqual(_service()._evaluate_freshness_decay(data)["status"], "ok")

    def test_iso_datetime_with_timezone_is_parsed(self):
        data = _page({"@type": "BlogPosting", "datePublished": "2026-01-10T08:30:00Z"})

        self.assertEqual(_service()._evaluate_freshness_decay(data)["value"]["published"], "2026-01-10")

    def test_missing_date_is_info(self):
        data = _page({"@type": "BlogPosting", "headline": "Tekst"})

        self.assertEqual(_service()._evaluate_freshness_decay(data)["status"], "info")


class ExternalSourcesTests(SimpleTestCase):
    ARTYKUL_URL = "https://sklep.pl/blog/poradnik"

    def test_trusted_followed_link_is_ok(self):
        data = _page(None, body='<a href="https://www.gov.pl/przepisy">przepisy</a>', url=self.ARTYKUL_URL)

        metric = _service()._evaluate_external_sources(data)

        self.assertEqual(metric["status"], "ok")
        self.assertIn("gov.pl", metric["value"]["hosts"])

    def test_article_without_sources_is_a_warning(self):
        data = _page(None, body="<p>Treść bez źródeł.</p>", url=self.ARTYKUL_URL)

        self.assertEqual(_service()._evaluate_external_sources(data)["status"], "warning")

    def test_nofollow_only_sources_are_a_warning(self):
        data = _page(None, body='<a href="https://www.gov.pl/x" rel="nofollow">x</a>', url=self.ARTYKUL_URL)

        self.assertEqual(_service()._evaluate_external_sources(data)["status"], "warning")

    def test_non_article_page_is_info(self):
        data = _page(None, body="<p>Oferta</p>", url="https://sklep.pl/produkt/kosz")

        self.assertEqual(_service()._evaluate_external_sources(data)["status"], "info")

    def test_internal_links_are_not_counted_as_outbound(self):
        data = _page(None, body='<a href="https://sklep.pl/inna">wewnętrzny</a>', url=self.ARTYKUL_URL)

        self.assertEqual(data["outbound_links"]["total"], 0)


# ----------------------------------------------------------------------
# MODUŁ 3: GEO Suppression
# ----------------------------------------------------------------------

class HiddenContentTests(SimpleTestCase):
    OPINIE = " ".join(["Wspolpraca", "przebiega", "wzorowo", "od", "lat"] * 12)

    def test_large_hidden_block_is_critical(self):
        data = _page(None, body=f'<div class="reviews d-none">{self.OPINIE}</div><p>krótko</p>')

        metric = _service()._evaluate_hidden_content(data)

        self.assertEqual(metric["status"], "error")
        self.assertGreater(metric["value"]["share"], 0.3)

    def test_inline_display_none_is_detected(self):
        data = _page(None, body=f'<div style="display:none">{self.OPINIE}</div>')

        self.assertGreater(_service()._evaluate_hidden_content(data)["value"]["blocks"], 0)

    def test_hidden_attribute_is_detected(self):
        data = _page(None, body=f"<section hidden>{self.OPINIE}</section>")

        self.assertGreater(_service()._evaluate_hidden_content(data)["value"]["blocks"], 0)

    def test_visible_content_is_ok(self):
        data = _page(None, body=f"<div>{self.OPINIE}</div>")

        self.assertEqual(_service()._evaluate_hidden_content(data)["status"], "ok")

    def test_small_hidden_block_is_ignored(self):
        """Ukryty tooltip czy etykieta nie są utratą treści dla AI."""
        data = _page(None, body='<div class="d-none">krótka etykieta</div>')

        self.assertEqual(_service()._evaluate_hidden_content(data)["status"], "ok")

    def test_nested_hidden_blocks_are_counted_once(self):
        data = _page(None, body=f'<div class="d-none"><div class="inner">{self.OPINIE}</div></div>')

        self.assertEqual(_service()._evaluate_hidden_content(data)["value"]["blocks"], 1)


# ----------------------------------------------------------------------
# Testy autorskie
# ----------------------------------------------------------------------

class HeadingVisibilityTests(SimpleTestCase):
    def test_hidden_no_results_heading_is_critical(self):
        """Wzorzec FreshGift: ukryty H6 z komunikatem o braku produktów pod poprawnym H1."""
        data = _page(None, body='<h1>Kosze</h1><h6 style="display:none">Nie znaleziono produktów</h6><h2>Oferta</h2>')

        metric = _service()._evaluate_heading_visibility(data)

        self.assertEqual(metric["status"], "error")
        self.assertEqual(metric["value"]["hidden_count"], 1)

    def test_other_hidden_heading_is_a_warning(self):
        data = _page(None, body='<h1>Tytuł</h1><h3 class="sr-only">Nawigacja pomocnicza</h3>')

        self.assertEqual(_service()._evaluate_heading_visibility(data)["status"], "warning")

    def test_all_visible_headings_are_ok(self):
        data = _page(None, body="<h1>Tytuł</h1><h2>Sekcja</h2>")

        self.assertEqual(_service()._evaluate_heading_visibility(data)["status"], "ok")


class SchemaHtmlParityTests(SimpleTestCase):
    def test_faq_schema_without_html_faq_is_critical(self):
        """Wzorzec FreshGift: FAQPage w Schema bez sekcji pytań w HTML."""
        data = _page({"@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": "Jak?"}]},
                     body="<h1>Strona główna</h1><p>Zwykła treść.</p>")

        metric = _service()._evaluate_schema_html_parity(data)

        self.assertEqual(metric["status"], "error")
        self.assertTrue(any("FAQPage" in m for m in metric["value"]["mismatches"]))

    def test_product_schema_without_visible_price_is_critical(self):
        data = _page({"@type": "Product", "name": "Kosz"}, body="<h1>Kosz</h1>")

        metric = _service()._evaluate_schema_html_parity(data)

        self.assertTrue(any("Product" in m for m in metric["value"]["mismatches"]))

    def test_matching_declarations_are_ok(self):
        data = _page({"@type": "Product", "name": "Kosz"},
                     body='<h1>Kosz</h1><span class="price">129,00 zł</span>')

        self.assertEqual(_service()._evaluate_schema_html_parity(data)["status"], "ok")


class PlaceholderContentTests(SimpleTestCase):
    def test_lorem_ipsum_in_body_is_critical(self):
        data = _page(None, body="<p>Lorem ipsum dolor sit amet.</p>")

        metric = _service()._evaluate_placeholder_content(data)

        self.assertEqual(metric["status"], "error")

    def test_placeholder_in_schema_is_detected(self):
        data = _page({"@type": "Product", "description": "Opis w przygotowaniu"}, body="<p>Treść.</p>")

        self.assertEqual(_service()._evaluate_placeholder_content(data)["status"], "error")

    def test_placeholder_in_heading_is_detected(self):
        data = _page(None, body="<h1>TODO: uzupełnić tytuł</h1>")

        self.assertEqual(_service()._evaluate_placeholder_content(data)["status"], "error")

    def test_clean_page_is_ok(self):
        data = _page(None, body="<h1>Kosze prezentowe</h1><p>Oferujemy kosze dla firm.</p>")

        self.assertEqual(_service()._evaluate_placeholder_content(data)["status"], "ok")


class StructuredContentThresholdTests(SimpleTestCase):
    """Progi udziału struktur zależne od typu podstrony: produkt 15%, artykuł 10%."""

    def _body(self, listy: int, akapity: int) -> str:
        return "<ul><li>x</li></ul>" * listy + "<p>Akapit treści.</p>" * akapity

    def test_product_page_uses_higher_threshold(self):
        # 1 lista na 9 akapitów = 10% - wystarczy dla artykułu, za mało dla produktu.
        produkt = _page(None, body=self._body(1, 9), url="https://sklep.pl/produkt/kosz")
        artykul = _page(None, body=self._body(1, 9), url="https://sklep.pl/blog/poradnik")

        self.assertEqual(_service()._evaluate_structured_content(produkt)["status"], "warning")
        self.assertEqual(_service()._evaluate_structured_content(artykul)["status"], "ok")

    def test_definition_lists_count_as_structure(self):
        data = _page(None, body="<dl><dt>Waga</dt><dd>2 kg</dd></dl><p>Opis.</p>")

        structured = data["structured_content"]
        self.assertEqual(structured["definition_lists"], 1)
        self.assertEqual(structured["definition_pairs"], 1)

    def test_prose_only_product_page_is_a_warning(self):
        data = _page(None, body="<p>Opis.</p>" * 10, url="https://sklep.pl/produkt/kosz")

        self.assertEqual(_service()._evaluate_structured_content(data)["status"], "warning")


class NewMetricsRegistrationTests(SimpleTestCase):
    NOWE = (
        "schema_entity_linking", "price_discrepancy", "schema_data_hygiene",
        "ecommerce_completeness", "authorship_depth", "freshness_decay",
        "external_sources", "hidden_content", "heading_visibility",
        "schema_html_parity", "placeholder_content",
    )

    def test_all_new_keys_are_registered_in_ui(self):
        from auditor.presentation import (
            METRIC_DEFINITIONS,
            OFFICIAL_TEST_NAMES,
            TECHNICAL_ACCORDIONS,
        )

        klucze = set().union(*(keys for _, _, keys in TECHNICAL_ACCORDIONS))
        for key in self.NOWE:
            with self.subTest(metryka=key):
                self.assertIn(key, METRIC_DEFINITIONS)
                self.assertIn(key, OFFICIAL_TEST_NAMES)
                self.assertIn(key, klucze)

    def test_all_new_metrics_share_the_same_shape(self):
        data = _page({"@type": "Product", "name": "Kosz"}, body='<span class="price">99,00 zł</span>')
        service = _service()
        metryki = [
            service._evaluate_schema_entity_linking(data),
            service._evaluate_price_discrepancy(data),
            service._evaluate_schema_data_hygiene(data),
            service._evaluate_ecommerce_completeness(data),
            service._evaluate_authorship_depth(data),
            service._evaluate_freshness_decay(data),
            service._evaluate_external_sources(data),
            service._evaluate_hidden_content(data),
            service._evaluate_heading_visibility(data),
            service._evaluate_schema_html_parity(data),
            service._evaluate_placeholder_content(data),
        ]

        for metric in metryki:
            with self.subTest(metryka=metric["key"]):
                self.assertEqual({"category", "key", "value", "status", "current_value"}, set(metric))
                self.assertIn(metric["status"], ("ok", "warning", "error", "info"))
                self.assertTrue(metric["value"]["note"])
