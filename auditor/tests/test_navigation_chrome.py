"""Widoczność paska narzędzi i panelu użytkownika."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

BAR_MARKER = 'class="tool-bar"'
BACK_MARKER = "Powrót do Hubu narzędzi"
LOGOUT_MARKER = "Wyloguj"
USERNAME_MARKER = 'class="header-username"'
LOGO_MARKER = "SEO Auditor"
LOGIN_FORM_MARKER = 'name="password"'


class LoginScreenChromeTests(TestCase):
    """Ekran logowania ma zostać czysty, reszta aplikacji - z pełną nawigacją."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="nawigacja-test",
            password="haslo-kontrolne-1",
        )

    def test_login_page_hides_tool_bar_for_anonymous_user(self):
        html = self.client.get("/login/").content.decode()

        self.assertNotIn(BAR_MARKER, html)
        self.assertNotIn(BACK_MARKER, html)

    def test_login_page_hides_logout_button_for_anonymous_user(self):
        html = self.client.get("/login/").content.decode()

        self.assertNotIn(LOGOUT_MARKER, html)
        self.assertNotIn(USERNAME_MARKER, html)

    def test_login_page_keeps_logo_and_form(self):
        html = self.client.get("/login/").content.decode()

        self.assertIn(LOGO_MARKER, html)
        self.assertIn(LOGIN_FORM_MARKER, html)

    def test_login_page_stays_clean_for_authenticated_user(self):
        # Zalogowany użytkownik też bywa na /login/ - np. gdy przełącza konto.
        # Pasek narzędzi nad formularzem byłby wtedy tylko szumem.
        self.client.force_login(self.user)

        html = self.client.get("/login/").content.decode()

        self.assertNotIn(BAR_MARKER, html)
        self.assertNotIn(LOGOUT_MARKER, html)


class ToolBarVisibilityTests(TestCase):
    """Pasek powrotu pojawia się w panelach, ale nie na samym hubie."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="pasek-test",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_hub_has_no_tool_bar(self):
        # Na hubie link "wróć do hubu" prowadziłby do tej samej strony.
        html = self.client.get(reverse("auditor:hub")).content.decode()

        self.assertNotIn(BAR_MARKER, html)
        self.assertNotIn(BACK_MARKER, html)

    def test_hub_still_shows_user_panel(self):
        html = self.client.get(reverse("auditor:hub")).content.decode()

        self.assertIn(LOGOUT_MARKER, html)
        self.assertIn("pasek-test", html)

    def test_scanner_panel_shows_back_link(self):
        html = self.client.get(reverse("auditor:index")).content.decode()

        self.assertIn(BAR_MARKER, html)
        self.assertIn(BACK_MARKER, html)

    def test_geo_panel_shows_back_link(self):
        html = self.client.get(reverse("auditor:geo_dashboard")).content.decode()

        self.assertIn(BAR_MARKER, html)
        self.assertIn(BACK_MARKER, html)

    def test_analytics_panel_shows_back_link(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn(BAR_MARKER, html)
        self.assertIn(BACK_MARKER, html)

    def test_switcher_offers_the_other_two_tools(self):
        html = self.client.get(reverse("auditor:geo_dashboard")).content.decode()

        self.assertIn("Analityka i Ruch", html)
        self.assertIn("Audyt Techniczny", html)
        # Bieżące narzędzie nie jest wymienione w przełączniku.
        switcher = html.split('class="tool-bar-switch"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("Widoczność w AI", switcher)

    def test_switcher_links_are_not_underlined(self):
        # Reguła `header a` nie obejmuje paska - bez własnej reguły linki wracają
        # do domyślnego, podkreślonego stylu przeglądarki.
        html = self.client.get(reverse("auditor:index")).content.decode()

        rule = html.split(".tool-bar-switch-item {", 1)[1].split("}", 1)[0]
        self.assertIn("text-decoration: none", rule)


class NavigationContextProcessorTests(TestCase):
    """Sam procesor - bez pełnego renderowania szablonu."""

    def test_flag_is_false_without_resolver_match(self):
        # Strony błędów renderują się zanim Django dopasuje trasę.
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auditor.context_processors import navigation

        request = RequestFactory().get("/dowolny-adres/")
        request.user = AnonymousUser()

        self.assertFalse(navigation(request)["nav_visible"])

    def test_flag_is_false_without_user_attribute(self):
        from django.test import RequestFactory

        from auditor.context_processors import navigation

        request = RequestFactory().get("/")

        context = navigation(request)

        self.assertFalse(context["nav_visible"])
        self.assertFalse(context["user_panel_visible"])


class NavSectionResolverTests(TestCase):
    """Mapowanie ścieżek na sekcje - bez renderowania."""

    def test_geo_paths(self):
        from auditor.navigation import resolve_nav_section

        self.assertEqual(resolve_nav_section("/geo-visibility/"), "geo")
        self.assertEqual(resolve_nav_section("/geo-visibility/7/"), "geo")

    def test_analytics_paths(self):
        from auditor.navigation import resolve_nav_section

        self.assertEqual(resolve_nav_section("/analytics/"), "analytics")
        self.assertEqual(resolve_nav_section("/ga4/callback/"), "analytics")
        self.assertEqual(resolve_nav_section("/gsc/raport/"), "analytics")

    def test_technical_paths(self):
        from auditor.navigation import resolve_nav_section

        self.assertEqual(resolve_nav_section("/audits/"), "technical")
        self.assertEqual(resolve_nav_section("/audits/12/"), "technical")

    def test_root_is_the_hub(self):
        from auditor.navigation import resolve_nav_section

        self.assertEqual(resolve_nav_section("/"), "hub")

    def test_path_outside_the_app_has_no_section(self):
        from auditor.navigation import resolve_nav_section

        self.assertIsNone(resolve_nav_section("bez-ukosnika"))

    def test_other_tools_skips_the_current_one(self):
        from auditor.navigation import other_tools

        keys = [tool["key"] for tool in other_tools("geo")]

        self.assertEqual(keys, ["analytics", "technical"])

    def test_other_tools_on_hub_returns_all_three(self):
        from auditor.navigation import other_tools

        self.assertEqual(len(other_tools("hub")), 3)
