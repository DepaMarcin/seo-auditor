"""Izolacja danych między kontami i uprawnienia administratora.

Audyty i badania GEO zawierają dane analityczne klienta (GA4, Search Console, lista
konkurencji), więc sama znajomość identyfikatora nie może wystarczać do odczytania
cudzego raportu. Odpowiedzią jest 404, nie 403: identyczna reakcja na "nie istnieje"
i "nie twoje" nie pozwala ustalić, które identyfikatory są zajęte.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit, GeoQuery, GeoStudy

User = get_user_model()

# `validate_public_url` woła prawdziwy DNS, a adresy w domenie .example celowo się
# nie rozwiązują. Bez tej podmiany formularz odrzucałby adres i testy własności
# mierzyłyby walidację zamiast przypisania właściciela.
_dns_patch = None


def _fake_getaddrinfo(hostname, *args, **kwargs):
    import ipaddress

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        address = "93.184.216.34"
    else:
        address = hostname
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


def setUpModule():
    global _dns_patch
    _dns_patch = patch("auditor.services.url_guard.socket.getaddrinfo", _fake_getaddrinfo)
    _dns_patch.start()


def tearDownModule():
    _dns_patch.stop()


class AuditIsolationTests(TestCase):
    """Użytkownik A nie widzi audytów użytkownika B."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        cls.bogdan = User.objects.create_user(username="bogdan", password="haslo-kontrolne-2")
        cls.audyt_ali = Audit.objects.create(url="https://ala.example/", owner=cls.ala)
        cls.audyt_bogdana = Audit.objects.create(url="https://bogdan.example/", owner=cls.bogdan)

    def setUp(self):
        self.client.force_login(self.ala)

    def test_foreign_audit_detail_returns_404(self):
        response = self.client.get(
            reverse("auditor:detail", args=[self.audyt_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_foreign_audit_status_returns_404(self):
        response = self.client.get(
            reverse("auditor:status", args=[self.audyt_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_foreign_audit_export_returns_404(self):
        response = self.client.get(
            reverse("auditor:export_report", args=[self.audyt_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_foreign_audit_pdf_returns_404(self):
        response = self.client.get(
            reverse("auditor:download_pdf_report", args=[self.audyt_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_own_audit_is_accessible(self):
        response = self.client.get(reverse("auditor:detail", args=[self.audyt_ali.pk]))

        self.assertEqual(response.status_code, 200)

    def test_listing_shows_only_own_audits(self):
        response = self.client.get(reverse("auditor:index"))

        self.assertEqual(list(response.context["audits"]), [self.audyt_ali])

    def test_analytics_panel_shows_only_own_audits(self):
        response = self.client.get(reverse("auditor:analytics"))

        wiersze = list(response.context["connected_audits"]) + list(
            response.context["pending_audits"]
        )
        self.assertEqual([row["audit"] for row in wiersze], [self.audyt_ali])


class AuditOwnershipTests(TestCase):
    """Nowy audyt dostaje właściciela z sesji, a nie z formularza."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        cls.bogdan = User.objects.create_user(username="bogdan", password="haslo-kontrolne-2")

    def setUp(self):
        self.client.force_login(self.ala)

    def test_created_audit_belongs_to_the_logged_in_user(self):
        with patch("auditor.views.enqueue_audit"):
            self.client.post(reverse("auditor:index"), {"url": "https://nowa.example/"})

        audit = Audit.objects.get()
        self.assertEqual(audit.owner, self.ala)

    def test_owner_cannot_be_forced_through_the_form(self):
        # Podrzucenie cudzego identyfikatora w POST nie może zmienić właściciela.
        with patch("auditor.views.enqueue_audit"):
            self.client.post(reverse("auditor:index"), {
                "url": "https://nowa.example/",
                "owner": self.bogdan.pk,
            })

        self.assertEqual(Audit.objects.get().owner, self.ala)

    def test_created_geo_study_belongs_to_the_logged_in_user(self):
        with patch("auditor.tasks.enqueue_geo_study"):
            self.client.post(reverse("auditor:geo_dashboard"), {
                "domain": "nowa.example",
                "questions": ["Jaka firma?"],
            })

        self.assertEqual(GeoStudy.objects.get().owner, self.ala)


class GeoStudyIsolationTests(TestCase):
    """Badania GEO podlegają tej samej izolacji co audyty."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        cls.bogdan = User.objects.create_user(username="bogdan", password="haslo-kontrolne-2")
        cls.badanie_ali = GeoStudy.objects.create(owner=cls.ala, domain="ala.example")
        cls.badanie_bogdana = GeoStudy.objects.create(owner=cls.bogdan, domain="bogdan.example")
        GeoQuery.objects.create(study=cls.badanie_bogdana, text="Pytanie?", position=1)

    def setUp(self):
        self.client.force_login(self.ala)

    def test_foreign_study_detail_returns_404(self):
        response = self.client.get(
            reverse("auditor:geo_detail", args=[self.badanie_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_foreign_study_status_returns_404(self):
        response = self.client.get(
            reverse("auditor:geo_status", args=[self.badanie_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_foreign_study_cannot_be_rerun(self):
        response = self.client.post(
            reverse("auditor:geo_rerun", args=[self.badanie_bogdana.pk])
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(GeoStudy.objects.count(), 2)

    def test_dashboard_lists_only_own_studies(self):
        response = self.client.get(reverse("auditor:geo_dashboard"))

        self.assertEqual(list(response.context["studies"]), [self.badanie_ali])


class SuperuserVisibilityTests(TestCase):
    """Superużytkownik widzi wszystko - i tak ma dostęp przez /admin/."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        cls.admin = User.objects.create_superuser(
            username="admin", password="haslo-kontrolne-3"
        )
        cls.audyt_ali = Audit.objects.create(url="https://ala.example/", owner=cls.ala)
        cls.badanie_ali = GeoStudy.objects.create(owner=cls.ala, domain="ala.example")

    def setUp(self):
        self.client.force_login(self.admin)

    def test_superuser_opens_any_audit(self):
        response = self.client.get(reverse("auditor:detail", args=[self.audyt_ali.pk]))

        self.assertEqual(response.status_code, 200)

    def test_superuser_opens_any_geo_study(self):
        response = self.client.get(
            reverse("auditor:geo_detail", args=[self.badanie_ali.pk])
        )

        self.assertEqual(response.status_code, 200)

    def test_superuser_listing_includes_foreign_audits(self):
        response = self.client.get(reverse("auditor:index"))

        self.assertIn(self.audyt_ali, response.context["audits"])

    def test_regular_user_still_cannot_see_foreign_data(self):
        # Wyjątek dotyczy wyłącznie konta administracyjnego.
        self.client.force_login(self.ala)
        cudzy = Audit.objects.create(url="https://admin.example/", owner=self.admin)

        response = self.client.get(reverse("auditor:detail", args=[cudzy.pk]))

        self.assertEqual(response.status_code, 404)


class PasswordChangeTests(TestCase):
    """Widok zmiany własnego hasła pod /change-password/."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="stare-haslo-kontrolne-1")

    def setUp(self):
        self.client.force_login(self.ala)

    def test_page_renders_with_the_application_template(self):
        response = self.client.get("/change-password/")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/change_password.html")
        self.assertContains(response, "Zmiana hasła")

    def test_password_is_changed(self):
        response = self.client.post("/change-password/", {
            "old_password": "stare-haslo-kontrolne-1",
            "new_password1": "nowe-haslo-kontrolne-9",
            "new_password2": "nowe-haslo-kontrolne-9",
        })

        self.assertRedirects(response, "/change-password/done/")
        self.ala.refresh_from_db()
        self.assertTrue(self.ala.check_password("nowe-haslo-kontrolne-9"))

    def test_wrong_current_password_is_rejected(self):
        self.client.post("/change-password/", {
            "old_password": "nie-to-haslo",
            "new_password1": "nowe-haslo-kontrolne-9",
            "new_password2": "nowe-haslo-kontrolne-9",
        })

        self.ala.refresh_from_db()
        self.assertTrue(self.ala.check_password("stare-haslo-kontrolne-1"))

    def test_mismatched_new_passwords_are_rejected(self):
        self.client.post("/change-password/", {
            "old_password": "stare-haslo-kontrolne-1",
            "new_password1": "nowe-haslo-kontrolne-9",
            "new_password2": "zupelnie-inne-haslo-8",
        })

        self.ala.refresh_from_db()
        self.assertTrue(self.ala.check_password("stare-haslo-kontrolne-1"))

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.get("/change-password/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class HeaderLinksTests(TestCase):
    """Linki konta w nagłówku zależą od uprawnień."""

    @classmethod
    def setUpTestData(cls):
        cls.ala = User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        cls.admin = User.objects.create_superuser(
            username="admin", password="haslo-kontrolne-3"
        )

    def test_regular_user_sees_change_password_but_not_admin_panel(self):
        self.client.force_login(self.ala)

        html = self.client.get(reverse("auditor:hub")).content.decode()

        self.assertIn("Zmień hasło", html)
        self.assertIn('href="/change-password/"', html)
        self.assertNotIn("Panel Admina", html)

    def test_superuser_sees_the_admin_panel_link(self):
        self.client.force_login(self.admin)

        html = self.client.get(reverse("auditor:hub")).content.decode()

        self.assertIn("Panel Admina", html)
        self.assertIn('href="/admin/"', html)

    def test_login_page_shows_no_account_links(self):
        html = self.client.get("/login/").content.decode()

        self.assertNotIn("Zmień hasło", html)
        self.assertNotIn("Panel Admina", html)


class AdminRegistrationTests(TestCase):
    """Administrator zarządza kontami i danymi z panelu /admin/."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="admin", password="haslo-kontrolne-3"
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_user_management_is_available(self):
        response = self.client.get("/admin/auth/user/")

        self.assertEqual(response.status_code, 200)

    def test_user_can_be_created_from_the_panel(self):
        response = self.client.get("/admin/auth/user/add/")

        self.assertEqual(response.status_code, 200)

    def test_audits_are_registered_with_their_owner(self):
        Audit.objects.create(url="https://ala.example/", owner=self.admin)

        html = self.client.get("/admin/auditor/audit/").content.decode()

        self.assertIn("admin", html)

    def test_geo_studies_are_registered(self):
        response = self.client.get("/admin/auditor/geostudy/")

        self.assertEqual(response.status_code, 200)

    def test_regular_user_cannot_enter_the_admin_panel(self):
        self.client.force_login(
            User.objects.create_user(username="ala", password="haslo-kontrolne-1")
        )

        response = self.client.get("/admin/", follow=True)

        # Django przekierowuje na ekran logowania panelu zamiast wpuścić.
        self.assertNotContains(response, "Panel Admina")
        self.assertEqual(response.status_code, 200)
