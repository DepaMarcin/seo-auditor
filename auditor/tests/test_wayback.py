"""Testy integracji z Wayback Machine (wiek domeny) oraz panelu podsumowania PageSpeed.

Wszystkie zapytania HTTP do Internet Archive są mockowane - żaden test nie łączy się
z siecią ani nie zależy od limitów publicznego API.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, AuditMetric
from auditor.presentation import (
    extract_pagespeed_summary,
    group_technical_accordions,
    pagespeed_score_bucket,
)
from auditor.services.audit_service import (
    DOMAIN_AGE_ESTABLISHED_YEARS,
    AuditService,
)
from auditor.services.wayback import CDX_API_URL, WaybackService


def _service() -> AuditService:
    rag = MagicMock()
    rag.generate_recommendation.return_value = "Rekomendacja testowa."
    return AuditService(rag_engine=rag)


def _cdx_response(timestamp: str | None) -> httpx.Response:
    """Odpowiedź CDX: pierwszy wiersz to nagłówek kolumn, dane od drugiego."""
    rows = [["timestamp", "original"]]
    if timestamp:
        rows.append([timestamp, "http://example.com:80/"])
    return httpx.Response(200, request=httpx.Request("GET", CDX_API_URL), json=rows)


def _mock_client(get_return=None, get_side_effect=None):
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if get_side_effect is not None:
        client.get.side_effect = get_side_effect
    else:
        client.get.return_value = get_return
    return client


class WaybackServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.service = WaybackService()

    def test_returns_first_snapshot_and_age(self):
        with patch("auditor.services.wayback.httpx.Client", return_value=_mock_client(_cdx_response("19970501120000"))):
            result = self.service.fetch_domain_history("https://example.com")

        self.assertTrue(result["available"])
        self.assertTrue(result["archived"])
        self.assertEqual(result["first_snapshot"], "1997-05-01")
        self.assertGreater(result["age_years"], 25)

    def test_domain_absent_from_archive(self):
        with patch("auditor.services.wayback.httpx.Client", return_value=_mock_client(_cdx_response(None))):
            result = self.service.fetch_domain_history("https://nowa-domena.pl")

        self.assertTrue(result["available"])
        self.assertFalse(result["archived"])
        self.assertIsNone(result["first_snapshot"])

    def test_strips_www_and_scheme_from_domain(self):
        client = _mock_client(_cdx_response("20100330000000"))
        with patch("auditor.services.wayback.httpx.Client", return_value=client):
            self.service.fetch_domain_history("https://www.example.com/podstrona?a=1")

        self.assertEqual(client.get.call_args.kwargs["params"]["url"], "example.com")

    def test_network_error_falls_back_to_availability_api(self):
        """Gdy CDX zawiedzie, sięgamy po zapasowe /wayback/available."""
        availability = httpx.Response(
            200,
            request=httpx.Request("GET", "http://archive.org/wayback/available"),
            json={"archived_snapshots": {"closest": {"available": True, "timestamp": "20050101000000",
                                                     "url": "http://web.archive.org/web/2005/example.com"}}},
        )
        with patch(
            "auditor.services.wayback.httpx.Client",
            side_effect=[
                _mock_client(get_side_effect=httpx.ReadTimeout("timeout", request=httpx.Request("GET", CDX_API_URL))),
                _mock_client(availability),
            ],
        ):
            result = self.service.fetch_domain_history("https://example.com")

        self.assertTrue(result["archived"])
        self.assertEqual(result["first_snapshot"], "2005-01-01")

    def test_rate_limit_is_reported_as_unavailable_not_as_new_domain(self):
        """429 to limit archiwum, a nie dowód, że domena jest nowa."""
        too_many = httpx.Response(429, request=httpx.Request("GET", "http://archive.org/wayback/available"))
        with patch(
            "auditor.services.wayback.httpx.Client",
            side_effect=[
                _mock_client(get_side_effect=httpx.ReadTimeout("timeout", request=httpx.Request("GET", CDX_API_URL))),
                _mock_client(get_side_effect=httpx.HTTPStatusError("429", request=too_many.request, response=too_many)),
            ],
        ):
            result = self.service.fetch_domain_history("https://example.com")

        self.assertFalse(result["available"])
        self.assertFalse(result["archived"])
        self.assertIn("ogranicza", result["error"])

    def test_successful_result_is_cached(self):
        client = _mock_client(_cdx_response("20100330000000"))
        with patch("auditor.services.wayback.httpx.Client", return_value=client):
            self.service.fetch_domain_history("https://example.com")
            self.service.fetch_domain_history("https://example.com")

        self.assertEqual(client.get.call_count, 1)

    def test_malformed_timestamp_is_treated_as_not_archived(self):
        with patch("auditor.services.wayback.httpx.Client", return_value=_mock_client(_cdx_response("nonsens"))):
            result = self.service.fetch_domain_history("https://example.com")

        self.assertFalse(result["archived"])


class WaybackMetricTests(SimpleTestCase):
    """Logika statusów: OK dla domen z historią, INFO/WARNING dla młodych."""

    def _history(self, years: float) -> dict:
        first = date.today() - timedelta(days=int(years * 365.25))
        return {
            "available": True, "archived": True, "first_snapshot": first.isoformat(),
            "age_days": int(years * 365.25), "age_years": years,
            "snapshot_url": "http://web.archive.org/web/x", "error": None,
        }

    def test_established_domain_is_ok(self):
        metric = _service()._evaluate_wayback_domain_age(self._history(DOMAIN_AGE_ESTABLISHED_YEARS + 5))

        self.assertEqual(metric["key"], "wayback_domain_age")
        self.assertEqual(metric["status"], "ok")

    def test_moderately_young_domain_is_info(self):
        metric = _service()._evaluate_wayback_domain_age(self._history(1.0))

        self.assertEqual(metric["status"], "info")

    def test_very_young_domain_is_warning_with_authority_advice(self):
        metric = _service()._evaluate_wayback_domain_age(self._history(0.2))

        self.assertEqual(metric["status"], "warning")
        self.assertIn("autorytet", metric["value"]["note"])

    def test_domain_missing_from_archive_is_warning(self):
        metric = _service()._evaluate_wayback_domain_age({
            "available": True, "archived": False, "first_snapshot": None,
            "age_years": None, "age_days": None, "snapshot_url": None, "error": None,
        })

        self.assertEqual(metric["status"], "warning")

    def test_unavailable_archive_is_info_not_an_error(self):
        """Niedostępność archiwum nie może obniżać oceny audytowanej strony."""
        metric = _service()._evaluate_wayback_domain_age({
            "available": False, "archived": False, "first_snapshot": None,
            "age_years": None, "age_days": None, "snapshot_url": None, "error": "limit",
        })

        self.assertEqual(metric["status"], "info")

    def test_metric_shape_matches_other_checks(self):
        metric = _service()._evaluate_wayback_domain_age(self._history(3.0))

        self.assertEqual({"category", "key", "value", "status", "current_value"}, set(metric))
        self.assertEqual(metric["category"], "technical")
        self.assertTrue(metric["value"]["note"])


class WaybackAccordionTests(TestCase):
    """Nowy klucz musi trafić do akordeonu "Indeksacja, Renderowanie & Nawigacja"."""

    def test_metric_lands_in_indexing_accordion(self):
        audit = Audit.objects.create(url="https://example.com", status=Audit.Status.COMPLETED)
        AuditMetric.objects.create(
            audit=audit, category="technical", key="wayback_domain_age", status="ok",
            value={"note": "Domena ma ugruntowaną historię."},
        )

        from auditor.presentation import annotate_metric_labels

        groups = group_technical_accordions(annotate_metric_labels(list(audit.metrics.all())))
        indexing = next(g for g in groups if g["id"] == "indexing")

        self.assertIn("wayback_domain_age", {m.short_key for m in indexing["metrics"]})

    def test_metric_has_definition_and_official_name(self):
        from auditor.presentation import METRIC_DEFINITIONS, OFFICIAL_TEST_NAMES

        self.assertIn("wayback_domain_age", METRIC_DEFINITIONS)
        self.assertIn("wayback_domain_age", OFFICIAL_TEST_NAMES)


class PagespeedSummaryPanelTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="pstester", password="haslo12345")
        self.client.force_login(self.user)
        self.audit = Audit.objects.create(
            url="https://example.com", status=Audit.Status.COMPLETED, score=70, owner=self.user
        )

    def _add_pagespeed(self, status="ok", mobile=95, desktop=98):
        AuditMetric.objects.create(
            audit=self.audit, category="performance", key="pagespeed_score", status=status,
            value={
                "label": f"Wynik PageSpeed Insights (Mobile: {mobile}/100, Desktop: {desktop}/100)",
                "mobile_score": mobile, "desktop_score": desktop, "note": "Wydajność w normie.",
            },
        )

    def test_score_buckets_follow_google_thresholds(self):
        self.assertEqual(pagespeed_score_bucket(95), "ok")
        self.assertEqual(pagespeed_score_bucket(90), "ok")
        self.assertEqual(pagespeed_score_bucket(89), "warning")
        self.assertEqual(pagespeed_score_bucket(50), "warning")
        self.assertEqual(pagespeed_score_bucket(49), "error")
        self.assertEqual(pagespeed_score_bucket(None), "warning")

    def test_panel_renders_with_both_scores(self):
        self._add_pagespeed(mobile=42, desktop=88)

        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))
        html = response.content.decode()

        self.assertIsNotNone(response.context["pagespeed_summary"])
        self.assertEqual(response.context["pagespeed_mobile_bucket"], "error")
        self.assertEqual(response.context["pagespeed_desktop_bucket"], "warning")
        # Selektor ze znacznikiem, a nie sama nazwa klasy - ta występuje też w <style>.
        self.assertIn('<div class="pagespeed-gauges">', html)

    def test_metric_is_removed_from_accordions_to_avoid_duplication(self):
        self._add_pagespeed()

        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))

        for group in response.context["technical_accordions"]:
            self.assertNotIn("pagespeed_score", {m.short_key for m in group["metrics"]}, group["id"])

    def test_card_is_shown_in_panel_when_status_is_ok(self):
        self._add_pagespeed(status="ok")

        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))

        self.assertTrue(response.context["pagespeed_card_in_panel"])

    def test_card_is_not_duplicated_when_metric_is_a_priority_finding(self):
        """Przy błędzie karta jest już w panelu priorytetów - panel PageSpeed jej nie powtarza."""
        self._add_pagespeed(status="error", mobile=20, desktop=35)

        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))

        self.assertFalse(response.context["pagespeed_card_in_panel"])
        self.assertIn(
            "pagespeed_score",
            {m.short_key for m in response.context["priority_findings"]},
        )

    def test_panel_is_absent_without_pagespeed_metric(self):
        response = self.client.get(reverse("auditor:detail", kwargs={"pk": self.audit.pk}))

        self.assertIsNone(response.context["pagespeed_summary"])
        self.assertNotContains(response, '<div class="pagespeed-gauges">')

    def test_extractor_returns_none_when_metric_missing(self):
        self.assertIsNone(extract_pagespeed_summary([]))
