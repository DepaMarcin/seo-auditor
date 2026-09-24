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


class AnalyticsPanelTests(TestCase):
    """Panel analityki rozdziela audyty na podłączone i niepodłączone do GA4."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="analityka-test",
            password="haslo-kontrolne-1",
        )
        cls.connected = Audit.objects.create(
            url="https://podlaczony.example/",
            owner=cls.user,
            ga4_property_id="123456789",
            ga4_organic_sessions=4321,
        )
        cls.pending = Audit.objects.create(
            url="https://niepodlaczony.example/",
            owner=cls.user,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_panel_renders(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/analytics.html")

    def test_connected_audit_is_listed_with_its_property(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(list(response.context["connected_audits"]), [self.connected])
        self.assertContains(response, "123456789")
        self.assertContains(response, "4321")

    def test_pending_audit_is_listed_separately(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(list(response.context["pending_audits"]), [self.pending])
        self.assertContains(response, "Połącz GA4")

    def test_items_link_into_the_report_analytics_section(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn(f'href="/audits/{self.connected.pk}/#analityka"', html)

    def test_other_users_audits_are_not_listed(self):
        intruder = get_user_model().objects.create_user(
            username="obcy",
            password="haslo-kontrolne-2",
        )
        self.client.force_login(intruder)

        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(list(response.context["connected_audits"]), [])
        self.assertEqual(list(response.context["pending_audits"]), [])
        self.assertFalse(response.context["has_any_audit"])

    def test_panel_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
