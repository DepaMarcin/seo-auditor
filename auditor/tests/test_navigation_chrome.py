"""Widoczność paska nawigacji i panelu użytkownika."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.test import TestCase

NAV_MARKER = 'class="main-nav"'
LOGOUT_MARKER = "Wyloguj"
USERNAME_MARKER = 'class="header-username"'
LOGO_MARKER = "SEO Auditor"
LOGIN_FORM_MARKER = 'name="password"'


class NavigationVisibilityTests(TestCase):
    """Ekran logowania ma zostać czysty, reszta aplikacji - z pełną nawigacją."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="nawigacja-test",
            password="haslo-kontrolne-1",
        )

    def test_login_page_hides_navigation_for_anonymous_user(self):
        html = self.client.get("/login/").content.decode()

        self.assertNotIn(NAV_MARKER, html)
        self.assertNotIn("Analityka i Ruch", html)
        self.assertNotIn("Audyt Techniczny", html)
        self.assertNotIn("Widoczność w AI", html)

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
        # Pasek sekcji nad formularzem byłby wtedy tylko szumem.
        self.client.force_login(self.user)

        html = self.client.get("/login/").content.decode()

        self.assertNotIn(NAV_MARKER, html)
        self.assertNotIn(LOGOUT_MARKER, html)

    def test_dashboard_shows_navigation_after_login(self):
        self.client.force_login(self.user)

        html = self.client.get("/").content.decode()

        self.assertIn(NAV_MARKER, html)
        self.assertIn("Analityka i Ruch", html)
        self.assertIn("Audyt Techniczny", html)
        self.assertIn("Widoczność w AI", html)

    def test_dashboard_shows_user_panel_after_login(self):
        self.client.force_login(self.user)

        html = self.client.get("/").content.decode()

        self.assertIn(LOGOUT_MARKER, html)
        self.assertIn(USERNAME_MARKER, html)
        self.assertIn("nawigacja-test", html)

    def test_geo_dashboard_shows_navigation(self):
        self.client.force_login(self.user)

        html = self.client.get(reverse("auditor:geo_dashboard")).content.decode()

        self.assertIn(NAV_MARKER, html)
        self.assertIn(LOGOUT_MARKER, html)


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


class NavigationActiveTabTests(TestCase):
    """Podświetlenie zakładki wynika z adresu strony."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="zakladki-test",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_home_marks_technical_tab(self):
        response = self.client.get("/")

        self.assertEqual(response.context["nav_section"], "technical")

    def test_geo_dashboard_marks_geo_tab(self):
        response = self.client.get(reverse("auditor:geo_dashboard"))

        self.assertEqual(response.context["nav_section"], "geo")

    def test_active_tab_is_rendered_with_accent_class(self):
        html = self.client.get(reverse("auditor:geo_dashboard")).content.decode()

        self.assertIn('class="main-nav-item active"', html)
        self.assertIn('aria-current="page"', html)

    def test_exactly_one_tab_is_active(self):
        html = self.client.get("/").content.decode()

        self.assertEqual(html.count('class="main-nav-item active"'), 1)
        self.assertEqual(html.count('aria-current="page"'), 1)

    def test_navigation_links_are_not_underlined(self):
        # Reguła `header a` nie obejmuje paska sekcji - bez własnej reguły linki
        # wracają do domyślnego, podkreślonego stylu przeglądarki.
        html = self.client.get("/").content.decode()

        self.assertIn(".main-nav-item {", html)
        nav_rule = html.split(".main-nav-item {", 1)[1].split("}", 1)[0]
        self.assertIn("text-decoration: none", nav_rule)


class NavSectionResolverTests(TestCase):
    """Mapowanie ścieżek na sekcje - bez renderowania."""

    def test_geo_paths(self):
        from auditor.context_processors import resolve_nav_section

        self.assertEqual(resolve_nav_section("/geo-visibility/"), "geo")
        self.assertEqual(resolve_nav_section("/geo-visibility/7/"), "geo")

    def test_analytics_paths(self):
        from auditor.context_processors import resolve_nav_section

        self.assertEqual(resolve_nav_section("/ga4/callback/"), "analytics")
        self.assertEqual(resolve_nav_section("/gsc/raport/"), "analytics")
        self.assertEqual(resolve_nav_section("/analytics/"), "analytics")

    def test_technical_paths(self):
        from auditor.context_processors import resolve_nav_section

        self.assertEqual(resolve_nav_section("/"), "technical")
        self.assertEqual(resolve_nav_section("/audits/12/"), "technical")

    def test_path_outside_the_app_has_no_section(self):
        from auditor.context_processors import resolve_nav_section

        self.assertIsNone(resolve_nav_section("bez-ukosnika"))
