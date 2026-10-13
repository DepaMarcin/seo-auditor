"""Cel konwersji GA4 konfigurowany z panelu „Analityka i Ruch”.

Formularz przyszedł tu z raportu technicznego: zdarzenie uznawane za lead opisuje
biznes, nie stan kodu strony, więc jego miejsce jest w module analitycznym.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount

User = get_user_model()


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów - nazwa klasy w CSS nie znaczy, że element istnieje."""
    return re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)


class LeadEventBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="cel@przyklad.pl",
            email="cel@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://enova.pl/", owner=cls.user, status=Audit.Status.COMPLETED,
            ga4_property_id="316375346", ga4_organic_sessions=14284,
            gsc_total_clicks_current=26311,
        )

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "jan@gmail.com"
        konto.save()

    def _open_panel(self, zdarzenia=("generate_lead", "purchase", "form_submit")):
        """Panel analityki z atrapami wszystkich wywołań Google."""
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=list(zdarzenia),
        ):
            return self.client.get(
                reverse("auditor:audit_analytics", args=[self.audit.pk])
            )

    def _save(self, event_name, audit=None, **dodatkowe):
        """Zapis celu z atrapą serwisu - zwraca (odpowiedź, atrapa serwisu)."""
        audit = audit or self.audit
        dane = {"ga4_selected_lead_event": event_name, **dodatkowe}

        def zapisz(audyt, credentials, nazwa):
            Audit.objects.filter(pk=audyt.pk).update(ga4_selected_lead_event=nazwa or "")
            return audyt

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.audit_service.AuditService.refresh_ga4_lead_event",
            side_effect=zapisz,
        ) as serwis:
            odpowiedz = self.client.post(
                reverse("auditor:set_lead_event", args=[audit.pk]), dane
            )
        return odpowiedz, serwis


class LeadEventFormTests(LeadEventBase):
    """Formularz celu w panelu analityki."""

    def test_panel_renders_the_form(self):
        response = self._open_panel()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="ga4-lead-form"')
        self.assertContains(
            response, f'action="/audits/{self.audit.pk}/lead-event/"'
        )
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_available_events_become_options(self):
        html = _body_only(self._open_panel().content.decode())

        for zdarzenie in ("generate_lead", "purchase", "form_submit"):
            with self.subTest(zdarzenie=zdarzenie):
                self.assertIn(f'value="{zdarzenie}"', html)

    def test_current_event_is_preselected(self):
        Audit.objects.filter(pk=self.audit.pk).update(
            ga4_selected_lead_event="purchase"
        )

        html = _body_only(self._open_panel().content.decode())

        self.assertRegex(html, r'value="purchase"\s*\n?\s*selected')

    def test_form_offers_clearing_the_goal(self):
        # "Nie śledzę konwersji" jest poprawnym stanem, nie brakiem wyboru.
        html = _body_only(self._open_panel().content.decode())

        self.assertIn('value=""', html)
        self.assertIn("nie śledzę konwersji", html)

    def test_missing_event_list_is_explained(self):
        html = _body_only(self._open_panel(zdarzenia=[]).content.decode())

        self.assertIn("Nie udało się pobrać listy zdarzeń", html)

    def test_form_returns_to_the_analytics_panel(self):
        html = _body_only(self._open_panel().content.decode())

        self.assertIn('name="next" value="analytics"', html)

    def test_event_list_is_cached_between_renders(self):
        # Bez cache każde wejście na panel pytałoby GA4 o 90 dni danych.
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=["generate_lead"],
        ) as zdarzenia:
            adres = reverse("auditor:audit_analytics", args=[self.audit.pk])
            self.client.get(adres)
            self.client.get(adres)

        self.assertEqual(zdarzenia.call_count, 1)

    def test_technical_report_no_longer_configures_the_goal(self):
        # Regresja w drugą stronę: konfiguracja celu nie może wrócić do audytu.
        html = _body_only(
            self.client.get(
                reverse("auditor:detail", args=[self.audit.pk])
            ).content.decode()
        )

        self.assertNotIn("ga4_selected_lead_event", html)
        self.assertNotIn("Śledzenie leadów", html)


class LeadEventSaveTests(LeadEventBase):
    """Zapis celu konwersji."""

    def test_event_is_persisted(self):
        odpowiedz, serwis = self._save("generate_lead")

        self.audit.refresh_from_db()
        self.assertEqual(self.audit.ga4_selected_lead_event, "generate_lead")
        self.assertTrue(serwis.called)
        self.assertEqual(serwis.call_args.args[2], "generate_lead")

    def test_empty_value_clears_the_goal(self):
        Audit.objects.filter(pk=self.audit.pk).update(
            ga4_selected_lead_event="purchase"
        )

        _, serwis = self._save("")

        self.audit.refresh_from_db()
        self.assertEqual(self.audit.ga4_selected_lead_event, "")
        # Serwis dostaje None, nie pusty napis - przelicza wtedy wnioski bez leadów.
        self.assertIsNone(serwis.call_args.args[2])

    def test_whitespace_is_not_an_event_name(self):
        _, serwis = self._save("   ")

        self.assertIsNone(serwis.call_args.args[2])

    def test_save_redirects_to_the_analytics_panel(self):
        odpowiedz, _ = self._save("generate_lead")

        self.assertRedirects(
            odpowiedz,
            reverse("auditor:audit_analytics", args=[self.audit.pk]),
            fetch_redirect_response=False,
        )

    def test_return_target_is_limited_to_known_routes(self):
        # `next` wybiera z listy nazwanych tras, nigdy nie jest surowym adresem -
        # inaczej formularz dałby się wykorzystać do przekierowania poza serwis.
        odpowiedz, _ = self._save("generate_lead", next="https://zly.example/")

        self.assertRedirects(
            odpowiedz,
            reverse("auditor:audit_analytics", args=[self.audit.pk]),
            fetch_redirect_response=False,
        )

    def test_detail_is_a_valid_return_target(self):
        odpowiedz, _ = self._save("generate_lead", next="detail")

        self.assertRedirects(
            odpowiedz,
            reverse("auditor:detail", args=[self.audit.pk]),
            fetch_redirect_response=False,
        )

    def test_success_message_names_the_goal(self):
        self._save("generate_lead")
        response = self.client.get(reverse("auditor:hub"))

        komunikaty = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("generate_lead" in m for m in komunikaty))

    def test_clearing_has_its_own_message(self):
        self._save("")
        response = self.client.get(reverse("auditor:hub"))

        komunikaty = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Przestano śledzić" in m for m in komunikaty))


class LeadEventGuardTests(LeadEventBase):
    """Czego widok zapisu nie może zrobić."""

    def test_get_is_not_allowed(self):
        # Zapis zmienia dane i wywołuje API Google - nie może dać się wywołać
        # samym otwarciem adresu.
        response = self.client.get(
            reverse("auditor:set_lead_event", args=[self.audit.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_another_users_audit_is_404(self):
        obcy = User.objects.create_user(
            username="obcy41@przyklad.pl",
            email="obcy41@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(
            url="https://cudza.pl/", owner=obcy, ga4_property_id="999"
        )

        response = self.client.post(
            reverse("auditor:set_lead_event", args=[cudzy.pk]),
            {"ga4_selected_lead_event": "generate_lead"},
        )

        self.assertEqual(response.status_code, 404)
        cudzy.refresh_from_db()
        self.assertFalse(cudzy.ga4_selected_lead_event)

    def test_without_a_connected_service_nothing_is_called(self):
        bez_uslugi = Audit.objects.create(url="https://inna.pl/", owner=self.user)

        with patch(
            "auditor.services.audit_service.AuditService.refresh_ga4_lead_event",
            side_effect=AssertionError("nie powinno być wywołane bez usługi"),
        ):
            response = self.client.post(
                reverse("auditor:set_lead_event", args=[bez_uslugi.pk]),
                {"ga4_selected_lead_event": "generate_lead"},
            )

        self.assertEqual(response.status_code, 302)
        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(any("Połącz najpierw" in m for m in komunikaty))

    def test_google_failure_does_not_show_an_error_page(self):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.audit_service.AuditService.refresh_ga4_lead_event",
            side_effect=RuntimeError("GA4 nie odpowiada"),
        ):
            response = self.client.post(
                reverse("auditor:set_lead_event", args=[self.audit.pk]),
                {"ga4_selected_lead_event": "generate_lead"},
            )

        self.assertEqual(response.status_code, 302)
        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(any("Nie udało się pobrać danych" in m for m in komunikaty))

    def test_expired_session_is_reported(self):
        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            side_effect=KeyError("brak klucza"),
        ), patch(
            "auditor.services.audit_service.AuditService.refresh_ga4_lead_event",
            side_effect=AssertionError("nie powinno być wywołane bez poświadczeń"),
        ):
            response = self.client.post(
                reverse("auditor:set_lead_event", args=[self.audit.pk]),
                {"ga4_selected_lead_event": "generate_lead"},
            )

        self.assertEqual(response.status_code, 302)
        komunikaty = [
            str(m) for m in self.client.get(reverse("auditor:hub")).context["messages"]
        ]
        self.assertTrue(any("Sesja Google wygasła" in m for m in komunikaty))

    def test_view_requires_login(self):
        self.client.logout()

        response = self.client.post(
            reverse("auditor:set_lead_event", args=[self.audit.pk]),
            {"ga4_selected_lead_event": "generate_lead"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class AnalyticsRangeIntegrationTests(LeadEventBase):
    """Spięcie panelu z endpointem `/audits/<pk>/analytics-data/`."""

    def test_panel_carries_the_endpoint_address(self):
        # Adres idzie atrybutem `data-`, nie literałem w JS - tak jak dane wykresów.
        response = self._open_panel()

        self.assertContains(
            response,
            f'data-analytics-url="/audits/{self.audit.pk}/analytics-data/"',
        )

    def test_quick_ranges_are_rendered(self):
        html = _body_only(self._open_panel().content.decode())

        for zakres in ("7d", "30d", "90d", "12m"):
            with self.subTest(zakres=zakres):
                self.assertIn(f'data-range="{zakres}"', html)

    def test_updatable_numbers_have_identifiers(self):
        html = _body_only(self._open_panel().content.decode())

        self.assertIn('id="analytics-sessions-value"', html)
        self.assertIn('id="analytics-clicks-value"', html)

    def test_date_inputs_cannot_reach_the_future(self):
        from django.utils import timezone

        html = _body_only(self._open_panel().content.decode())

        self.assertIn(f'max="{timezone.localdate().isoformat()}"', html)

    def test_endpoint_answers_for_a_range(self):
        with patch(
            "auditor.views._build_ga4_payload",
            return_value={"available": True, "organic_sessions": 999, "lead_insights": {}},
        ), patch("auditor.views._build_gsc_payload", return_value={"available": False}):
            response = self.client.get(
                reverse("auditor:analytics_data", args=[self.audit.pk]),
                {"range": "30d"},
            )

        self.assertEqual(response.status_code, 200)
        dane = response.json()
        self.assertEqual(dane["ga4"]["organic_sessions"], 999)
        self.assertIn("label", dane["range"])

    def test_endpoint_rejects_a_broken_range(self):
        response = self.client.get(
            reverse("auditor:analytics_data", args=[self.audit.pk]),
            {"start_date": "2026-13-45", "end_date": "2026-01-01"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_endpoint_is_owner_scoped(self):
        obcy = User.objects.create_user(
            username="obcy42@przyklad.pl",
            email="obcy42@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudza.pl/", owner=obcy)

        response = self.client.get(
            reverse("auditor:analytics_data", args=[cudzy.pk]), {"range": "30d"}
        )

        self.assertEqual(response.status_code, 404)
