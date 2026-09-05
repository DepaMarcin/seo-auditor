"""Testy widoków Django: formularz nowego audytu, widok szczegółów oraz autoryzacja.

Zlecanie audytu (`auditor.views.enqueue_audit`) jest mockowane, aby żaden test widoku nie
wykonywał prawdziwego scrapowania, zapytań do PageSpeed Insights ani do OpenAI/ChromaDB.
Podmieniany jest też `validate_public_url` - walidator odpytuje DNS, a testy widoków
sprawdzają zachowanie widoku, nie walidatora (ten ma własny zestaw w `test_url_guard.py`).
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit, AuditMetric


class AuthenticatedTestCase(TestCase):
    """Baza: zalogowany użytkownik i czysty cache (licznik rate limitu, cache GA4)."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = User.objects.create_user(username="tester", password="haslo12345")
        self.client.force_login(self.user)


class LoginRequiredTests(TestCase):
    """Każdy widok audytora wymaga zalogowania - anonim jest przekierowany na logowanie."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(username="wlasciciel", password="haslo12345")
        self.audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=80, owner=self.owner
        )

    def test_anonymous_is_redirected_from_every_view(self):
        urls = [
            reverse("auditor:index"),
            reverse("auditor:detail", kwargs={"pk": self.audit.pk}),
            reverse("auditor:status", kwargs={"pk": self.audit.pk}),
            reverse("auditor:download_pdf_report", kwargs={"audit_id": self.audit.pk}),
            reverse("auditor:start_ga4_auth", kwargs={"pk": self.audit.pk}),
            reverse("auditor:select_ga4_property", kwargs={"pk": self.audit.pk}),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])


class AuditOwnershipTests(AuthenticatedTestCase):
    """Audyt należący do innego użytkownika jest niedostępny (ochrona przed IDOR)."""

    def setUp(self):
        super().setUp()
        self.other_user = User.objects.create_user(username="obcy", password="haslo12345")
        self.foreign_audit = Audit.objects.create(
            url="https://konkurencja.pl", status=Audit.Status.COMPLETED, score=70, owner=self.other_user
        )

    def test_detail_of_foreign_audit_returns_404(self):
        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.foreign_audit.pk}))

        # Świadomie 404, a nie 403 - inaczej kod odpowiedzi zdradzałby, które audyty istnieją.
        self.assertEqual(response.status_code, 404)

    def test_pdf_of_foreign_audit_returns_404(self):
        response = self.client.get(
            reverse("auditor:download_pdf_report", kwargs={"audit_id": self.foreign_audit.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_status_of_foreign_audit_returns_404(self):
        response = self.client.get(reverse("auditor:status", kwargs={"pk": self.foreign_audit.pk}))

        self.assertEqual(response.status_code, 404)

    def test_index_lists_only_own_audits(self):
        own = Audit.objects.create(url="https://moja-domena.pl", owner=self.user)

        response = self.client.get(reverse("auditor:index"))

        self.assertContains(response, "https://moja-domena.pl")
        self.assertNotContains(response, "https://konkurencja.pl")
        self.assertEqual(list(response.context["audits"]), [own])


class IndexViewGetTests(AuthenticatedTestCase):
    def test_get_renders_form_and_audit_list(self):
        Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=80, owner=self.user
        )

        response = self.client.get(reverse("auditor:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/index.html")
        self.assertContains(response, "https://example.com")

    def test_index_shows_at_most_ten_newest_audits(self):
        for i in range(12):
            Audit.objects.create(url=f"https://example-{i}.com", owner=self.user)

        response = self.client.get(reverse("auditor:index"))

        self.assertEqual(len(response.context["audits"]), 10)


@patch("auditor.views.validate_public_url", side_effect=lambda url: url or "")
@patch("auditor.views.enqueue_audit")
class IndexViewPostTests(AuthenticatedTestCase):
    def test_post_with_valid_url_creates_audit_and_redirects_to_detail(self, mock_enqueue, _mock_guard):
        response = self.client.post(reverse("auditor:index"), {"url": "https://example.com"})

        self.assertEqual(Audit.objects.count(), 1)
        audit = Audit.objects.get()
        self.assertEqual(audit.url, "https://example.com")
        self.assertEqual(audit.owner, self.user)
        self.assertRedirects(response, reverse("auditor:detail", kwargs={"pk": audit.pk}))
        mock_enqueue.assert_called_once_with(audit.pk)

    def test_post_never_runs_audit_synchronously(self, mock_enqueue, _mock_guard):
        """Widok zleca audyt do wykonania w tle - nie wykonuje go w wątku HTTP."""
        self.client.post(reverse("auditor:index"), {"url": "https://example.com"})

        mock_enqueue.assert_called_once()

    def test_post_with_empty_url_does_not_create_audit(self, mock_enqueue, mock_guard):
        from auditor.services.url_guard import UnsafeUrlError

        mock_guard.side_effect = UnsafeUrlError("Podaj adres URL do audytu.")

        response = self.client.post(reverse("auditor:index"), {"url": ""})

        self.assertEqual(Audit.objects.count(), 0)
        mock_enqueue.assert_not_called()
        self.assertRedirects(response, reverse("auditor:index"))

    def test_post_with_unsafe_url_does_not_create_audit(self, mock_enqueue, mock_guard):
        from auditor.services.url_guard import UnsafeUrlError

        mock_guard.side_effect = UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi.")

        response = self.client.post(reverse("auditor:index"), {"url": "http://127.0.0.1:8000/admin/"})

        self.assertEqual(Audit.objects.count(), 0)
        mock_enqueue.assert_not_called()
        self.assertRedirects(response, reverse("auditor:index"))

    def test_rate_limit_blocks_excessive_audits(self, mock_enqueue, _mock_guard):
        """Po przekroczeniu limitu kolejne zlecenia nie tworzą audytu ani nie idą do kolejki."""
        with self.settings(AUDIT_RATE_LIMIT_COUNT=2, AUDIT_RATE_LIMIT_WINDOW_SECONDS=3600):
            for _ in range(3):
                self.client.post(reverse("auditor:index"), {"url": "https://example.com"})

        self.assertEqual(Audit.objects.count(), 2)
        self.assertEqual(mock_enqueue.call_count, 2)


class AuditDetailViewTests(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=62, owner=self.user
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

    def test_errors_and_warnings_are_listed_before_passed_tests(self):
        """Kolejność kart w akordeonie: najpierw problemy, potem testy zdane."""
        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))

        for group in response.context["technical_accordions"]:
            statuses = [m.status for m in group["metrics"]]
            problem_positions = [i for i, s in enumerate(statuses) if s in ("error", "warning")]
            passed_positions = [i for i, s in enumerate(statuses) if s in ("ok", "info")]
            if problem_positions and passed_positions:
                self.assertLess(max(problem_positions), min(passed_positions), group["id"])

    def test_html_contains_recommendation_text(self):
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        self.assertContains(response, "Dodaj meta description.")

    def test_schema_status_table_shows_breadcrumblist_row(self):
        """schema_breadcrumbs nie jest już renderowany jako zwykła karta testu z
        surowym `note` - trafia do dedykowanej tabeli statusów Schema.org (patrz
        `build_schema_status_table`), pokazującej ikonę/etykietę statusu."""
        url = reverse("auditor:detail", kwargs={"pk": self.audit.pk})

        response = self.client.get(url)

        schema_names = {row["name"] for row in response.context["schema_status_table"]}
        self.assertIn("BreadcrumbList", schema_names)

    def test_failed_audit_shows_failure_message_without_crashing(self):
        failed_audit = Audit.objects.create(
            url="https://blad.pl", status=Audit.Status.FAILED, score=0, owner=self.user
        )
        url = reverse("auditor:detail", kwargs={"pk": failed_audit.pk})

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "błędem")

    def test_detail_view_404_for_missing_audit(self):
        response = self.client.get(reverse("auditor:detail", kwargs={"pk": 999999}))

        self.assertEqual(response.status_code, 404)


class AuditStatusEndpointTests(AuthenticatedTestCase):
    """Endpoint odpytywany przez stronę szczegółów, dopóki audyt liczy się w tle."""

    def test_returns_not_finished_while_processing(self):
        audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.PROCESSING, owner=self.user
        )

        response = self.client.get(reverse("auditor:status", kwargs={"pk": audit.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "processing")
        self.assertFalse(response.json()["finished"])

    def test_returns_finished_for_completed_audit(self):
        audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=88, owner=self.user
        )

        payload = self.client.get(reverse("auditor:status", kwargs={"pk": audit.pk})).json()

        self.assertTrue(payload["finished"])
        self.assertEqual(payload["score"], 88)

    def test_detail_page_shows_progress_panel_while_processing(self):
        audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.PROCESSING, owner=self.user
        )

        response = self.client.get(reverse("auditor:detail", kwargs={"pk": audit.pk}))

        self.assertTrue(response.context["audit_in_progress"])
        self.assertContains(response, "Trwa audyt strony")
