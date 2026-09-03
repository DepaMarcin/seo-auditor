"""Testy widoków Django: formularz nowego audytu oraz widok szczegółów audytu.

`AuditService.run_audit` jest tu mockowany, aby żaden test widoku nie wykonywał
prawdziwego scrapowania, zapytań do PageSpeed Insights ani do OpenAI/ChromaDB.
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit, AuditMetric


class IndexViewGetTests(TestCase):
    def test_get_renders_form_and_audit_list(self):
        Audit.objects.create(url="https://example.com", status=Audit.Status.COMPLETED, score=80)

        response = self.client.get(reverse("auditor:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/index.html")
        self.assertContains(response, "https://example.com")


class IndexViewPostTests(TestCase):
    @patch("auditor.views.AuditService")
    def test_post_with_valid_url_creates_audit_and_redirects_to_detail(self, mock_audit_service_cls):
        mock_service = mock_audit_service_cls.return_value

        response = self.client.post(reverse("auditor:index"), {"url": "https://example.com"})

        self.assertEqual(Audit.objects.count(), 1)
        audit = Audit.objects.get()
        self.assertEqual(audit.url, "https://example.com")
        self.assertRedirects(response, reverse("auditor:detail", kwargs={"pk": audit.pk}))
        mock_service.run_audit.assert_called_once_with(audit)

    @patch("auditor.views.AuditService")
    def test_post_never_hits_real_audit_service_internals(self, mock_audit_service_cls):
        """Upewnia się, że widok korzysta z (zamockowanej) klasy AuditService, a nie
        wykonuje żadnej prawdziwej logiki sieciowej bezpośrednio w widoku."""
        self.client.post(reverse("auditor:index"), {"url": "https://example.com"})

        mock_audit_service_cls.assert_called_once_with()

    def test_post_with_empty_url_does_not_create_audit(self):
        response = self.client.post(reverse("auditor:index"), {"url": ""})

        self.assertEqual(Audit.objects.count(), 0)
        self.assertRedirects(response, reverse("auditor:index"))

    def test_post_with_blank_whitespace_url_does_not_create_audit(self):
        response = self.client.post(reverse("auditor:index"), {"url": "   "})

        self.assertEqual(Audit.objects.count(), 0)
        self.assertRedirects(response, reverse("auditor:index"))


class AuditDetailViewTests(TestCase):
    def setUp(self):
        self.audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=62
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="seo",
            key="meta_description",
            status="error",
            value={"note": "Brak meta description.", "recommendation": "Dodaj meta description."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="technical",
            key="canonical",
            status="warning",
            value={"note": "Brak znacznika canonical.", "recommendation": "Dodaj canonical."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="technical",
            key="h1_structure",
            status="ok",
            value={"note": "Struktura H1 jest prawidłowa."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="structure",
            key="schema_breadcrumbs",
            status="warning",
            value={"note": "Brak danych strukturalnych BreadcrumbList na stronie."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="performance",
            key="pagespeed_score",
            status="ok",
            value={
                "label": "Wynik PageSpeed Insights (Mobile: 90/100, Desktop: 95/100)",
                "mobile_score": 90,
                "desktop_score": 95,
                "note": "Wydajność w normie.",
            },
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="performance",
            key="mobile_pagespeed_score",
            status="ok",
            value={"value": 90, "unit": "", "label": "Wynik PageSpeed", "note": "OK."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="performance",
            key="desktop_pagespeed_score",
            status="ok",
            value={"value": 95, "unit": "", "label": "Wynik PageSpeed", "note": "OK."},
        )
        AuditMetric.objects.create(
            audit=self.audit,
            category="performance",
            key="mobile_lcp",
            status="ok",
            value={"value": 1.2, "unit": "s", "label": "LCP", "note": "LCP: 1.20s (dobry wynik)."},
        )

    def test_get_renders_detail_template_with_200(self):
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/detail.html")

    def test_metrics_are_mapped_by_status_in_context(self):
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        critical_keys = {m.key for m in response.context["critical_errors"]}
        warning_keys = {m.key for m in response.context["warnings"]}
        self.assertIn("meta_description", critical_keys)
        self.assertIn("canonical", warning_keys)
        self.assertIn("schema_breadcrumbs", warning_keys)
        # Metryki score per urządzenie nie powinny duplikować zbiorczej metryki pagespeed_score.
        self.assertNotIn("mobile_pagespeed_score", {m.key for m in response.context["passed_tests"]})

    def test_stats_counts_match_summary_lists(self):
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)
        stats = response.context["stats"]

        self.assertEqual(stats["errors_count"], len(response.context["critical_errors"]))
        self.assertEqual(stats["warnings_count"], len(response.context["warnings"]))
        self.assertEqual(stats["passed_count"], len(response.context["passed_tests"]))

    def test_merged_pagespeed_score_in_technical_accordion(self):
        """Zbiorcza metryka "pagespeed_score" (z osadzonymi wynikami mobile/desktop
        w `value`) trafia do akordeonu "Obrazy, Wydajność & Bezpieczeństwo" zakładki
        Audyt Techniczny - dedykowane klucze kontekstu mobile_score/desktop_score
        nie istnieją już (zastąpione jednolitym grupowaniem akordeonowym); metryki
        score per urządzenie (mobile_/desktop_pagespeed_score) są z tej listy celowo
        wykluczone (MERGED_PAGESPEED_SCORE_KEYS), żeby nie dublować tej samej
        informacji, która i tak jest osadzona w `value` metryki zbiorczej."""
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        images_performance_group = next(
            g for g in response.context["technical_accordions"] if g["id"] == "images_performance"
        )
        pagespeed_metric = next(
            (m for m in images_performance_group["metrics"] if m.key == "pagespeed_score"), None
        )
        self.assertIsNotNone(pagespeed_metric)
        self.assertEqual(pagespeed_metric.value.get("mobile_score"), 90)
        self.assertEqual(pagespeed_metric.value.get("desktop_score"), 95)

    def test_html_contains_recommendation_text(self):
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        self.assertContains(response, "Dodaj meta description.")

    def test_schema_status_table_shows_breadcrumblist_row(self):
        """schema_breadcrumbs nie jest już renderowany jako zwykła karta testu z
        surowym `note` - trafia do dedykowanej tabeli statusów Schema.org (patrz
        `_build_schema_status_table`), pokazującej ikonę/etykietę statusu."""
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        schema_names = {row["name"] for row in response.context["schema_status_table"]}
        self.assertIn("BreadcrumbList", schema_names)

    def test_failed_audit_shows_failure_message_without_crashing(self):
        failed_audit = Audit.objects.create(url="https://blad.pl", status=Audit.Status.FAILED, score=0)
        url = reverse("auditor:detail", kwargs={"pk": failed_audit.pk})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "błędem")

    def test_detail_view_404_for_missing_audit(self):
        response = self.client.get(reverse("auditor:detail", kwargs={"pk": 999999}))

        self.assertEqual(response.status_code, 404)
