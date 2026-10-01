"""Analityka pojedynczego audytu - dwa stany, bez listy innych projektów.

Wcześniej zakładka "Analityka i Ruch" była globalną tablicą wszystkich audytów wraz
z ich stanami połączenia. Dane GA4 i GSC opisują zawsze jedną domenę, więc powrót
z autoryzacji Google na taką listę gubił kontekst pracy nad konkretnym projektem.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów - nazwa klasy w CSS nie znaczy, że element istnieje."""
    import re

    return re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)

User = get_user_model()


def _without_google():
    """Audyt bez połączenia - żadne wywołanie Google nie powinno się wydarzyć."""
    return patch(
        "auditor.services.google_services.build_credentials_from_refresh_token",
        side_effect=AssertionError("nie powinno być odpytywane bez tokenu"),
    )


class AnalyticsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="analityka@przyklad.pl",
            email="analityka@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.orlen = Audit.objects.create(url="https://orlen.pl/", owner=cls.user)
        cls.shell = Audit.objects.create(url="https://shell.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()

    def _connect(self, audit: Audit, property_id="222222") -> Audit:
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy",
            ga4_property_id=property_id,
            ga4_organic_sessions=4321,
            gsc_total_clicks_current=890,
            gsc_total_clicks_previous=700,
        )
        return Audit.objects.get(pk=audit.pk)

    def _open(self, audit: Audit):
        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[{"property_id": "222222", "display_name": "shell.pl", "account_name": "Konto"}],
        ), patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            return self.client.get(reverse("auditor:audit_analytics", args=[audit.pk]))


class EmptyStateTests(AnalyticsBase):
    """Stan 1: nic nie jest podłączone."""

    def test_page_names_the_domain(self):
        response = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak podłączonej analityki dla domeny")
        self.assertContains(response, "shell.pl")

    def test_connect_button_points_at_this_audit(self):
        html = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        ).content.decode()

        self.assertIn(f'href="/audits/{self.shell.pk}/ga4/connect/', html)
        self.assertIn("Podłącz Google Analytics i Search Console", html)

    def test_no_other_audits_are_listed(self):
        # Sedno przebudowy: ekran dotyczy jednej domeny.
        html = _body_only(
            self.client.get(
                reverse("auditor:audit_analytics", args=[self.shell.pk])
            ).content.decode()
        )

        self.assertNotIn("orlen.pl", html)

    def test_no_charts_or_numbers_in_the_empty_state(self):
        html = _body_only(
            self.client.get(
                reverse("auditor:audit_analytics", args=[self.shell.pk])
            ).content.decode()
        )

        self.assertNotIn("analytics-big-number", html)
        self.assertNotIn("Zmień usługę Google", html)

    def test_connected_account_without_property_asks_only_for_the_service(self):
        # Konto jest wspólne; brakuje wyłącznie wskazania usługi dla tej domeny.
        Audit.objects.filter(pk=self.shell.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        response = self._open(Audit.objects.get(pk=self.shell.pk))

        self.assertContains(response, "Wskaż usługę Google Analytics dla tej domeny")
        self.assertContains(response, "jan@gmail.com")

    def test_state_is_driven_by_the_property_not_the_token(self):
        Audit.objects.filter(pk=self.shell.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        response = self._open(Audit.objects.get(pk=self.shell.pk))

        self.assertFalse(response.context["has_analytics"])


class DataStateTests(AnalyticsBase):
    """Stan 2: usługa przypisana."""

    def test_numbers_are_shown(self):
        audit = self._connect(self.shell)

        response = self._open(audit)

        self.assertTrue(response.context["has_analytics"])
        self.assertContains(response, "4321")
        self.assertContains(response, "890")

    def test_assigned_services_are_visible(self):
        audit = self._connect(self.shell)

        response = self._open(audit)

        self.assertContains(response, "222222")

    def test_reconfiguration_link_is_discreet_but_present(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertIn("⚙️ Zmień usługę Google", html)
        self.assertIn(f'action="/analytics/{audit.pk}/assign/"', html)

    def test_empty_state_banner_is_gone(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertNotIn("Brak podłączonej analityki", html)

    def test_other_audits_still_absent(self):
        self._connect(self.shell)
        self._connect(self.orlen, property_id="111111")

        html = self._open(Audit.objects.get(pk=self.shell.pk)).content.decode()

        self.assertNotIn("orlen.pl", html)

    def test_link_back_to_the_report(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertIn(f'href="/audits/{audit.pk}/"', html)


class IsolationTests(AnalyticsBase):
    """Cudza analityka jest niedostępna."""

    def test_foreign_audit_returns_404(self):
        obcy = User.objects.create_user(
            username="obcy@przyklad.pl",
            email="obcy@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        response = self.client.get(
            reverse("auditor:audit_analytics", args=[cudzy.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()

        response = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class EntryPointTests(AnalyticsBase):
    """Wejście z hubu: wyłącznie wybór audytu."""

    def test_single_audit_goes_straight_to_its_analytics(self):
        Audit.objects.filter(pk=self.orlen.pk).delete()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_several_audits_show_a_plain_chooser(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/analytics_choose.html")
        self.assertContains(response, "shell.pl")
        self.assertContains(response, "orlen.pl")

    def test_chooser_shows_no_connection_state(self):
        # Wybór domeny to nie miejsce na banery i selektory usług.
        self._connect(self.shell)

        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertNotIn("Zalogowano do Google jako", html)
        self.assertNotIn('name="ga4_property_id"', html)
        self.assertNotIn("Brak podłączonej analityki", html)

    def test_chooser_links_to_per_audit_analytics(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn(f'href="/audits/{self.shell.pk}/analytics/"', html)
        self.assertIn(f'href="/audits/{self.orlen.pk}/analytics/"', html)

    def test_without_any_audit_user_is_sent_to_the_scanner(self):
        Audit.objects.all().delete()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertRedirects(
            response, reverse("auditor:index"), fetch_redirect_response=False
        )

    def test_only_own_audits_are_offered(self):
        obcy = User.objects.create_user(
            username="obcy2@przyklad.pl",
            email="obcy2@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertNotIn("cudzy.pl", html)


class RedirectAfterAssignmentTests(AnalyticsBase):
    """Po zapisie usługi użytkownik widzi skutek, nie listę."""

    def test_assignment_lands_on_this_audit_analytics(self):
        audit = self._connect(self.shell, property_id="999999")

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), \
             patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            response = self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": "222222", "gsc_site_url": ""},
            )

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[audit.pk]),
            fetch_redirect_response=False,
        )

    def test_oauth_switch_returns_to_this_audit_analytics(self):
        from auditor.views import _build_oauth_state

        flow = MagicMock()
        flow.credentials = MagicMock(refresh_token="token-odswiezania")
        state = _build_oauth_state(self.shell.pk, "losowy", "analytics")

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[{"property_id": "222", "display_name": "shell.pl"}],
             ):
            response = self.client.get("/ga4/callback/", {"state": state, "code": "kod"})

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_only_one_message_after_connecting(self):
        from auditor.views import _build_oauth_state

        flow = MagicMock()
        flow.credentials = MagicMock(refresh_token="token-odswiezania")
        state = _build_oauth_state(self.shell.pk, "losowy", "analytics")

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[{"property_id": "222", "display_name": "shell.pl"}],
             ):
            response = self.client.get(
                "/ga4/callback/", {"state": state, "code": "kod"}, follow=True
            )

        komunikaty = [m.message for m in response.context["messages"]]
        polaczenia = [m for m in komunikaty if "Połączono" in m]

        self.assertEqual(len(polaczenia), 1, f"komunikaty: {komunikaty}")

    def test_disconnect_returns_to_the_audit_it_came_from(self):
        audit = self._connect(self.shell)

        response = self.client.post(
            reverse("auditor:google_disconnect"), {"audit": str(audit.pk)}
        )

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[audit.pk]),
            fetch_redirect_response=False,
        )


class SelectorSuggestionTests(AnalyticsBase):
    """Selektory w karcie konfiguracji podpowiadają usługę pasującą do domeny.

    Te przypadki pokrywały wcześniej testy globalnego panelu - ich przedmiot
    przeniósł się razem z selektorami do widoku pojedynczego audytu.
    """

    def _open_with(self, audit, properties, sites):
        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=properties,
        ), patch("auditor.services.google_api.list_gsc_sites", return_value=sites):
            return self.client.get(reverse("auditor:audit_analytics", args=[audit.pk]))

    def test_matching_property_is_suggested_first(self):
        audit = self._connect(self.shell)

        response = self._open_with(
            audit,
            [
                {"property_id": "999", "display_name": "zupelnie inna", "account_name": "Konto"},
                {"property_id": "222222", "display_name": "shell.pl - GA4", "account_name": "Konto"},
            ],
            [],
        )

        opcje = response.context["selectors"]["ga4_options"]
        self.assertTrue(opcje[0]["suggested"])
        self.assertEqual(opcje[0]["value"], "222222")

    def test_matching_gsc_site_is_suggested_first(self):
        audit = self._connect(self.shell)

        response = self._open_with(
            audit,
            [],
            [
                {"site_url": "https://obca.pl/", "label": "https://obca.pl/", "domain": "obca.pl"},
                {"site_url": "sc-domain:shell.pl", "label": "shell.pl (usługa domenowa)", "domain": "shell.pl"},
            ],
        )

        opcje = response.context["selectors"]["gsc_options"]
        self.assertTrue(opcje[0]["suggested"])
        self.assertEqual(opcje[0]["value"], "sc-domain:shell.pl")

    def test_suggestion_is_labelled_in_the_markup(self):
        audit = self._connect(self.shell)

        html = self._open_with(
            audit,
            [{"property_id": "222222", "display_name": "shell.pl - GA4", "account_name": "Agencja"}],
            [],
        ).content.decode()

        self.assertIn("(Sugerowana)", html)
        self.assertIn("Agencja", html)

    def test_account_email_is_cached_on_the_audit(self):
        audit = self._connect(self.shell)

        self._open_with(audit, [], [])

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_account_email, "jan@gmail.com")

    def test_google_outage_does_not_break_the_page(self):
        audit = self._connect(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            side_effect=Exception("brak client_secret"),
        ):
            response = self.client.get(
                reverse("auditor:audit_analytics", args=[audit.pk])
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["google"]["connected"])

    def test_unreadable_token_is_treated_as_no_connection(self):
        audit = self._connect(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=None
        ):
            response = self.client.get(
                reverse("auditor:audit_analytics", args=[audit.pk])
            )

        self.assertFalse(response.context["google"]["connected"])
        self.assertEqual(response.context["google"]["error"], "token")
