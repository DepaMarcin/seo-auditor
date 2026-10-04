"""Hub narzędziowy pod `/` i panel analityki."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit


class HubViewTests(TestCase):
    """Ekran wyboru narzędzia po zalogowaniu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="hub-test",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_root_renders_the_hub(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/hub.html")

    def test_hub_shows_the_heading(self):
        html = self.client.get("/").content.decode()

        self.assertIn("Wybierz narzędzie", html)

    def test_hub_shows_three_cards(self):
        html = self.client.get("/").content.decode()

        self.assertEqual(html.count('class="hub-card hub-card-'), 3)

    def test_hub_names_all_three_tools(self):
        html = self.client.get("/").content.decode()

        self.assertIn("Analityka i Ruch", html)
        self.assertIn("Audyt Techniczny", html)
        self.assertIn("Widoczność w AI", html)

    def test_cards_carry_descriptions_and_calls_to_action(self):
        html = self.client.get("/").content.decode()

        self.assertIn("Śledź sesje, kliknięcia, CTR", html)
        self.assertIn("Głęboki skan SSR/CSR", html)
        self.assertIn("Badanie stochastyczne (5x5)", html)
        self.assertIn("Otwórz panel analityki", html)
        self.assertIn("Uruchom skaner SEO", html)
        self.assertIn("Uruchom symulator GEO", html)

    def test_cards_link_to_the_three_panels(self):
        html = self.client.get("/").content.decode()

        self.assertIn('href="/analytics/"', html)
        self.assertIn('href="/audits/"', html)
        self.assertIn('href="/geo-visibility/"', html)

    def test_hub_requires_login(self):
        self.client.logout()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class ScannerMovedTests(TestCase):
    """Skaner techniczny mieszka teraz pod /audits/."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="skaner-test",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_scanner_url_is_audits(self):
        self.assertEqual(reverse("auditor:index"), "/audits/")

    def test_scanner_still_renders_the_form(self):
        html = self.client.get("/audits/").content.decode()

        self.assertIn('id="audit-form"', html)
        self.assertIn("Uruchom audyt", html)

    def test_root_no_longer_shows_the_scanner_form(self):
        html = self.client.get("/").content.decode()

        self.assertNotIn('id="audit-form"', html)


class AnalyticsEntryTests(TestCase):
    """Kafelek analityki w hubie prowadzi na czysty ekran wejściowy.

    Bez listy audytów: ekran pyta o domenę i pokazuje wyłącznie te, do których
    zalogowane konto Google ma realny dostęp.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="analityka-test",
            password="haslo-kontrolne-1",
        )
        cls.audyt = Audit.objects.create(
            url="https://podlaczony.example/", owner=cls.user, ga4_property_id="123456789"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_entry_renders_the_search_screen(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/analytics_entry.html")

    def test_entry_does_not_list_audits(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertNotIn("podlaczony.example", html)

    def test_entry_without_google_offers_login(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn("Zaloguj przez Google", html)

    def test_entry_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
