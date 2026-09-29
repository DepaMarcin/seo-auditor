"""Logowanie adresem e-mail i wygląd panelu administracyjnego."""
from __future__ import annotations

from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.forms import EmailAuthenticationForm, EmailUserCreationForm

User = get_user_model()


class EmailBackendTests(TestCase):
    """Backend znajduje konto po adresie, niezależnie od pola, w którym on siedzi."""

    @classmethod
    def setUpTestData(cls):
        # Konto sprzed zmiany: nazwa użytkownika inna niż adres.
        cls.stare = User.objects.create_user(
            username="marcin", email="marcin@przyklad.pl", password="haslo-kontrolne-1"
        )
        # Konto założone w panelu: nazwa równa adresowi.
        cls.nowe = User.objects.create_user(
            username="anna@przyklad.pl",
            email="anna@przyklad.pl",
            password="haslo-kontrolne-2",
        )

    def test_login_by_email_for_an_older_account(self):
        user = authenticate(username="marcin@przyklad.pl", password="haslo-kontrolne-1")

        self.assertEqual(user, self.stare)

    def test_login_by_email_for_a_panel_account(self):
        user = authenticate(username="anna@przyklad.pl", password="haslo-kontrolne-2")

        self.assertEqual(user, self.nowe)

    def test_email_case_does_not_matter(self):
        user = authenticate(username="Marcin@Przyklad.PL", password="haslo-kontrolne-1")

        self.assertEqual(user, self.stare)

    def test_wrong_password_is_rejected(self):
        self.assertIsNone(
            authenticate(username="marcin@przyklad.pl", password="nie-to-haslo")
        )

    def test_unknown_email_is_rejected(self):
        self.assertIsNone(
            authenticate(username="nikt@przyklad.pl", password="haslo-kontrolne-1")
        )

    def test_inactive_account_cannot_log_in(self):
        self.stare.is_active = False
        self.stare.save(update_fields=["is_active"])

        self.assertIsNone(
            authenticate(username="marcin@przyklad.pl", password="haslo-kontrolne-1")
        )

    def test_duplicate_email_blocks_login(self):
        # Dwa konta na jeden adres to błąd danych; zgadywanie, które wpuścić,
        # byłoby gorsze niż odmowa.
        User.objects.create_user(
            username="marcin-drugi",
            email="marcin@przyklad.pl",
            password="haslo-kontrolne-3",
        )

        self.assertIsNone(
            authenticate(username="marcin@przyklad.pl", password="haslo-kontrolne-1")
        )


class LoginFormTests(TestCase):
    """Formularz logowania pyta o adres e-mail i pilnuje jego formatu."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="marcin", email="marcin@przyklad.pl", password="haslo-kontrolne-1"
        )

    def test_field_label_is_the_email_address(self):
        form = EmailAuthenticationForm()

        self.assertEqual(form.fields["username"].label, "Adres e-mail")

    def test_malformed_address_is_rejected_before_hitting_the_database(self):
        form = EmailAuthenticationForm(
            data={"username": "marcin", "password": "haslo-kontrolne-1"}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_valid_address_passes_validation(self):
        form = EmailAuthenticationForm(
            data={"username": "marcin@przyklad.pl", "password": "haslo-kontrolne-1"}
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_login_page_asks_for_an_email(self):
        html = self.client.get("/login/").content.decode()

        self.assertIn("Adres e-mail", html)
        self.assertIn('type="email"', html)
        self.assertNotIn(">Login<", html)

    def test_logging_in_through_the_page_works(self):
        response = self.client.post("/login/", {
            "username": "marcin@przyklad.pl",
            "password": "haslo-kontrolne-1",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_username_instead_of_email_is_refused_on_the_page(self):
        response = self.client.post("/login/", {
            "username": "marcin",
            "password": "haslo-kontrolne-1",
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class AdminAccountFormTests(TestCase):
    """Konta zakładane w panelu mają adres e-mail jako login."""

    def test_email_is_required(self):
        form = EmailUserCreationForm(data={
            "password1": "haslo-kontrolne-zZ9",
            "password2": "haslo-kontrolne-zZ9",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_username_is_set_from_the_email(self):
        form = EmailUserCreationForm(data={
            "email": "nowy@przyklad.pl",
            "password1": "haslo-kontrolne-zZ9",
            "password2": "haslo-kontrolne-zZ9",
        })
        self.assertTrue(form.is_valid(), form.errors)

        user = form.save()

        self.assertEqual(user.username, "nowy@przyklad.pl")
        self.assertEqual(user.email, "nowy@przyklad.pl")

    def test_created_account_can_log_in_by_email(self):
        form = EmailUserCreationForm(data={
            "email": "nowy@przyklad.pl",
            "password1": "haslo-kontrolne-zZ9",
            "password2": "haslo-kontrolne-zZ9",
        })
        form.is_valid()
        form.save()

        self.assertIsNotNone(
            authenticate(username="nowy@przyklad.pl", password="haslo-kontrolne-zZ9")
        )

    def test_duplicate_email_is_refused(self):
        User.objects.create_user(
            username="zajety@przyklad.pl",
            email="zajety@przyklad.pl",
            password="haslo-kontrolne-1",
        )

        form = EmailUserCreationForm(data={
            "email": "zajety@przyklad.pl",
            "password1": "haslo-kontrolne-zZ9",
            "password2": "haslo-kontrolne-zZ9",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


class AdminPanelTests(TestCase):
    """Panel administracyjny: dostęp i wygląd."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="admin@przyklad.pl",
            email="admin@przyklad.pl",
            password="haslo-kontrolne-3",
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_superuser_opens_the_panel(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)

    def test_header_carries_the_application_name(self):
        html = self.client.get("/admin/").content.decode()

        self.assertIn("SEO Auditor — Panel Administracyjny", html)
        self.assertNotIn("Django administration", html)

    def test_panel_uses_the_dark_palette(self):
        html = self.client.get("/admin/").content.decode()

        self.assertIn("--body-bg: #0B0F19", html)
        self.assertIn("--accent: #00E676", html)
        self.assertIn("--body-fg: #F8FAFC", html)

    def test_panel_links_back_to_the_application(self):
        html = self.client.get("/admin/").content.decode()

        self.assertIn("Powrót do aplikacji", html)

    def test_user_list_shows_email_addresses(self):
        html = self.client.get("/admin/auth/user/").content.decode()

        self.assertIn("admin@przyklad.pl", html)

    def test_add_user_form_asks_for_an_email(self):
        html = self.client.get("/admin/auth/user/add/").content.decode()

        self.assertIn("Adres e-mail", html)

    def test_user_created_through_the_panel_gets_matching_username(self):
        self.client.post("/admin/auth/user/add/", {
            "email": "panel@przyklad.pl",
            "password1": "haslo-kontrolne-zZ9",
            "password2": "haslo-kontrolne-zZ9",
        })

        user = User.objects.get(email="panel@przyklad.pl")
        self.assertEqual(user.username, "panel@przyklad.pl")

    def test_regular_user_is_bounced_from_the_panel(self):
        self.client.force_login(
            User.objects.create_user(
                username="ala@przyklad.pl",
                email="ala@przyklad.pl",
                password="haslo-kontrolne-1",
            )
        )

        response = self.client.get("/admin/", follow=True)

        # Django odsyła na ekran logowania panelu - ten sam nagłówek, ale bez
        # treści panelu, więc sprawdzamy przekierowanie, a nie nazwę serwisu.
        self.assertIn(("/admin/login/?next=/admin/", 302), response.redirect_chain)
        self.assertNotContains(response, "Badania GEO")
