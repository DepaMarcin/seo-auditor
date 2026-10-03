"""Co faktycznie trafia do przeglądarki: adresy w pętli i brak surowych komentarzy.

Dwa błędy, które te testy mają wyłapywać:

1. Wieloliniowy `{# ... #}` - w Django ten komentarz działa WYŁĄCZNIE w jednej linii.
   Rozbity na dwie wiersze trafia na ekran jako tekst.
2. Link w pętli wskazujący stały audyt zamiast bieżącego - użytkownik klikał przy
   jednej domenie, a konfigurował inną.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit

User = get_user_model()

TEMPLATE_ROOT = Path("auditor/templates")


class TemplateCommentSyntaxTests(SimpleTestCase):
    """Żaden szablon nie może zawierać wieloliniowego `{# #}`."""

    def test_no_multiline_hash_comments(self):
        bledne = []
        for sciezka in sorted(TEMPLATE_ROOT.rglob("*.html")):
            for numer, linia in enumerate(
                sciezka.read_text(encoding="utf-8").splitlines(), start=1
            ):
                # Komentarz otwarty i nie zamknięty w tej samej linii wycieka na ekran.
                if "{#" in linia and "#}" not in linia:
                    bledne.append(f"{sciezka}:{numer}: {linia.strip()}")

        self.assertEqual(bledne, [], "wieloliniowy {# #} wyświetli się użytkownikowi")

    def test_project_templates_too(self):
        katalog = Path("templates")
        if not katalog.exists():
            self.skipTest("brak katalogu szablonów projektu")

        bledne = [
            f"{sciezka}:{numer}"
            for sciezka in sorted(katalog.rglob("*.html"))
            for numer, linia in enumerate(
                sciezka.read_text(encoding="utf-8").splitlines(), start=1
            )
            if "{#" in linia and "#}" not in linia
        ]

        self.assertEqual(bledne, [])


class RenderedTemplateBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="render@przyklad.pl",
            email="render@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.orlen = Audit.objects.create(url="https://orlen.pl/", owner=cls.user)
        cls.shell = Audit.objects.create(url="https://shell.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()

    def _panel_with_account(self) -> str:
        """Panel analityki z podłączonym kontem Google (atrapy wywołań Google)."""
        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            return self.client.get(reverse("auditor:analytics")).content.decode()


class BannerReturnTests(RenderedTemplateBase):
    """Przełączenie konta wraca do panelu, nie do konfiguracji losowego audytu."""

    def test_connect_link_asks_to_return_to_analytics(self):
        html = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        ).content.decode()

        self.assertIn("return_to=analytics", html)
        self.assertIn(f"/audits/{self.shell.pk}/ga4/connect/", html)

    def test_switch_flow_lands_on_the_panel(self):
        from auditor.views import _build_oauth_state

        flow = MagicMock()
        flow.credentials = MagicMock(refresh_token="token-odswiezania")
        state = _build_oauth_state(self.orlen.pk, "losowy", "analytics")

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[{"property_id": "111", "display_name": "orlen.pl"}],
             ):
            response = self.client.get("/ga4/callback/", {"state": state, "code": "kod"})

        self.assertRedirects(
            response,
            reverse("auditor:audit_analytics", args=[self.orlen.pk]),
            fetch_redirect_response=False,
        )

    def test_row_flow_still_lands_on_the_selection_screen(self):
        from auditor.views import _build_oauth_state

        flow = MagicMock()
        flow.credentials = MagicMock(refresh_token="token-odswiezania")
        state = _build_oauth_state(self.shell.pk, "losowy")

        with patch("auditor.views.Flow.from_client_secrets_file", return_value=flow), \
             patch(
                 "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
                 return_value=[{"property_id": "222", "display_name": "shell.pl"}],
             ):
            response = self.client.get("/ga4/callback/", {"state": state, "code": "kod"})

        self.assertRedirects(
            response,
            reverse("auditor:select_ga4_property", args=[self.shell.pk]),
            fetch_redirect_response=False,
        )

    def test_state_still_carries_the_audit_number(self):
        from auditor.views import _audit_pk_from_state, _build_oauth_state

        state = _build_oauth_state(self.shell.pk, "losowy", "analytics")

        self.assertEqual(_audit_pk_from_state(state), self.shell.pk)

    def test_unknown_return_target_falls_back_to_the_selection_screen(self):
        from auditor.views import _build_oauth_state, _return_target_from_state

        state = _build_oauth_state(self.shell.pk, "losowy", "https://zlosliwa.example/")

        self.assertEqual(_return_target_from_state(state), "select")


class SelectionScreenRenderTests(RenderedTemplateBase):
    """Ekran wyboru usługi: właściwa domena i żadnych surowych komentarzy."""

    def _ekran(self, audit: Audit) -> str:
        cache.set(
            f"ga4_properties:{audit.pk}",
            [{"property_id": "222222", "display_name": "shell.pl"}],
            300,
        )
        return self.client.get(
            reverse("auditor:select_ga4_property", args=[audit.pk])
        ).content.decode()

    def test_heading_names_the_requested_audit(self):
        html = self._ekran(self.shell)

        naglowek = re.search(r"<h1>([^<]+)</h1>", html).group(1)

        self.assertIn("shell.pl", naglowek)
        self.assertNotIn("orlen.pl", naglowek)

    def test_no_raw_template_comment_reaches_the_browser(self):
        html = self._ekran(self.shell)

        self.assertNotIn("{#", html)
        self.assertNotIn("#}", html)
        self.assertNotIn("Witryna Search Console zostaje dopasowana", html)

    def test_form_action_points_at_this_audit(self):
        html = self._ekran(self.shell)

        self.assertIn(f'action="/analytics/{self.shell.pk}/assign/"', html)
        self.assertNotIn(f'action="/analytics/{self.orlen.pk}/assign/"', html)

    def test_hidden_fields_are_present_and_not_commented_out(self):
        html = self._ekran(self.shell)

        self.assertIn('name="next" value="detail"', html)
        # Pola witryny Search Console już nie ma - dobiera ją automat po domenie.
        self.assertNotIn('name="gsc_site_url"', html)
