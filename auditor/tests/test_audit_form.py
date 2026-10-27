"""Formularz nowego audytu po nieudanej walidacji.

Widok odpowiadał na błędny adres przekierowaniem na ten sam ekran. Przekierowanie
gubi dane POST, więc pole wracało puste i użytkownik przepisywał długi adres od zera
tylko po to, żeby zobaczyć ten sam komunikat. Formularz jest teraz renderowany
ponownie, z zachowanymi wartościami i błędem pod polem, którego dotyczy.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.services.url_guard import UnsafeUrlError


@patch("auditor.views.enqueue_audit")
class AuditFormValidationTests(TestCase):
    """Co widzi użytkownik, gdy adres nie przechodzi walidacji."""

    ADRES = reverse("auditor:index")

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = User.objects.create_user(username="tester", password="haslo12345")
        self.client.force_login(self.user)

    def _odrzuc(self, komunikat: str):
        """Strażnik adresów odrzucający KAŻDY adres podanym komunikatem."""
        return patch(
            "auditor.views.validate_public_url",
            side_effect=UnsafeUrlError(komunikat),
        )

    def test_the_rejected_address_stays_in_the_field(self, _mock_enqueue):
        wpisany = "https://przyklad.pl/bardzo/dluga/sciezka?parametr=wartosc"

        with self._odrzuc("Nie udało się rozwiązać nazwy domeny - sprawdź poprawność adresu."):
            response = self.client.post(self.ADRES, {"url": wpisany})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{wpisany}"')

    def test_the_reason_is_shown_next_to_the_field(self, _mock_enqueue):
        with self._odrzuc("Podaj poprawny adres zaczynający się od http:// lub https://."):
            response = self.client.post(self.ADRES, {"url": "to nie jest adres"})

        # Komunikat przy polu, nie tylko w globalnej liście na górze strony.
        self.assertContains(response, 'id="audit-url-error"')
        self.assertContains(response, "class=\"field-error\"")
        self.assertContains(response, "Podaj poprawny adres zaczynający się od http:// lub https://.")

    def test_the_field_is_marked_invalid_for_assistive_technology(self, _mock_enqueue):
        with self._odrzuc("Podaj adres URL do audytu."):
            response = self.client.post(self.ADRES, {"url": ""})

        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'aria-describedby="audit-url-error"')

    def test_each_cause_keeps_its_own_wording(self, _mock_enqueue):
        # Komunikat ma mówić, CO jest nie tak - "błędny adres" nie podpowiada niczego.
        przypadki = (
            "Podaj adres URL do audytu.",
            "Podaj poprawny adres zaczynający się od http:// lub https://.",
            "Adresy w sieci lokalnej nie podlegają audytowi.",
            "Nie udało się rozwiązać nazwy domeny - sprawdź poprawność adresu.",
        )
        for komunikat in przypadki:
            with self.subTest(komunikat=komunikat), self._odrzuc(komunikat):
                response = self.client.post(self.ADRES, {"url": "https://przyklad.pl"})

                self.assertContains(response, komunikat)

    def test_a_rejected_submission_creates_nothing(self, mock_enqueue):
        with self._odrzuc("Adresy w sieci lokalnej nie podlegają audytowi."):
            self.client.post(self.ADRES, {"url": "http://127.0.0.1:8000/admin/"})

        self.assertEqual(Audit.objects.count(), 0)
        mock_enqueue.assert_not_called()

    def test_a_valid_address_still_redirects_to_the_report(self, mock_enqueue):
        with patch("auditor.views.validate_public_url", side_effect=lambda url: url):
            response = self.client.post(self.ADRES, {"url": "https://przyklad.pl"})

        audit = Audit.objects.get()
        self.assertRedirects(response, reverse("auditor:detail", kwargs={"pk": audit.pk}))
        mock_enqueue.assert_called_once_with(audit.pk)

    def test_a_fresh_form_has_an_empty_field(self, _mock_enqueue):
        # Zachowywanie wartości nie może przecieknąć na zwykłe wejście na stronę.
        response = self.client.get(self.ADRES)

        self.assertContains(response, 'id="audit-url"')
        self.assertContains(response, 'value=""')
        self.assertNotContains(response, 'class="field-error"')


@patch("auditor.views.enqueue_audit")
class TemplateSlotValidationTests(TestCase):
    """Pola dodatkowych szablonów podstron - błąd trafia pod właściwe pole."""

    ADRES = reverse("auditor:index")

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = User.objects.create_user(username="tester", password="haslo12345")
        self.client.force_login(self.user)

    @staticmethod
    def _straznik(url: str) -> str:
        if "127.0.0.1" in url:
            raise UnsafeUrlError("Adresy w sieci lokalnej nie podlegają audytowi.")
        return url

    def test_the_error_points_at_the_offending_slot(self, _mock_enqueue):
        with patch("auditor.views.validate_public_url", side_effect=self._straznik):
            response = self.client.post(self.ADRES, {
                "url": "https://przyklad.pl",
                "url_category": "https://przyklad.pl/kategoria",
                "url_product": "http://127.0.0.1:8000/admin/",
            })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="error-url_product"')
        self.assertNotContains(response, 'id="error-url_category"')

    def test_all_entered_addresses_survive_the_error(self, _mock_enqueue):
        with patch("auditor.views.validate_public_url", side_effect=self._straznik):
            response = self.client.post(self.ADRES, {
                "url": "https://przyklad.pl",
                "url_category": "https://przyklad.pl/kategoria",
                "url_product": "http://127.0.0.1:8000/admin/",
            })

        for wartosc in (
            "https://przyklad.pl",
            "https://przyklad.pl/kategoria",
            "http://127.0.0.1:8000/admin/",
        ):
            with self.subTest(wartosc=wartosc):
                self.assertContains(response, f'value="{wartosc}"')

    def test_the_collapsed_section_opens_when_it_holds_an_error(self, _mock_enqueue):
        # Pola szablonów siedzą w `<details>`. Komunikat schowany w zwiniętej sekcji
        # jest dla użytkownika tym samym, co brak komunikatu.
        with patch("auditor.views.validate_public_url", side_effect=self._straznik):
            response = self.client.post(self.ADRES, {
                "url": "https://przyklad.pl",
                "url_product": "http://127.0.0.1:8000/admin/",
            })

        self.assertContains(response, 'id="template-slots" open')

    def test_the_section_stays_closed_on_a_fresh_form(self, _mock_enqueue):
        response = self.client.get(self.ADRES)

        self.assertNotContains(response, 'id="template-slots" open')
