"""Formularz podłączenia usług Google po przełączeniu konta.

Dymek „Search Console: automatycznie dopasowane do earlystage.pl" twierdził to
bezwarunkowo - także po przełączeniu na konto, które tej domeny w ogóle nie widzi.
Automat nie miał wtedy czego dopasować, a użytkownik dowiadywał się o tym dopiero
po zapisie, patrząc na pustą analitykę.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount

User = get_user_model()

WITRYNA_PASUJACA = {
    "site_url": "sc-domain:earlystage.pl",
    "label": "sc-domain:earlystage.pl",
    "domain": "earlystage.pl",
}
WITRYNA_OBCA = {
    "site_url": "sc-domain:innafirma.pl",
    "label": "sc-domain:innafirma.pl",
    "domain": "innafirma.pl",
}
GA4_PASUJACA = {
    "property_id": "316375346",
    "display_name": "earlystage.pl",
    "account_name": "Konto",
}
GA4_OBCA = {
    "property_id": "777777",
    "display_name": "innafirma.pl",
    "account_name": "Agencja",
}


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów, ze zwiniętymi białymi znakami."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


class ConnectFormBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="formularz@przyklad.pl",
            email="formularz@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        # Audyt BEZ przypisanej usługi - to stan, w którym widać ten formularz.
        cls.audit = Audit.objects.create(
            url="https://earlystage.pl/", owner=cls.user, status=Audit.Status.COMPLETED
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "hero5@gmail.com"
        konto.save()

    def _open(self, wlasciwosci=(), witryny=()):
        """Formularz widziany przez konto o podanych uprawnieniach."""
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="hero5@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=list(wlasciwosci),
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=list(witryny)
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            return self.client.get(
                reverse("auditor:audit_analytics", args=[self.audit.pk])
            )

    def _html(self, **kwargs):
        return _body_only(self._open(**kwargs).content.decode())


class HeadingTests(ConnectFormBase):
    """Nagłówki z danymi, nie z tekstem redakcyjnym."""

    def test_heading_shows_the_bare_domain(self):
        html = self._html(witryny=[WITRYNA_PASUJACA])

        self.assertIn("Wskaż usługi dla domeny earlystage.pl", html)

    def test_heading_does_not_show_the_full_address(self):
        # Schemat i ukośnik niosły w nagłówku tylko szum.
        html = self._html(witryny=[WITRYNA_PASUJACA])

        self.assertNotIn("Wskaż usługi dla domeny https://earlystage.pl/", html)

    def test_data_headings_opt_out_of_capitalisation(self):
        # Globalna reguła `h2 { text-transform: capitalize }` zamieniała
        # "earlystage.pl" w "Earlystage.Pl" - nagłówki z danymi muszą ją wyłączać.
        from pathlib import Path

        from django.conf import settings

        arkusz = (
            Path(settings.BASE_DIR) / "auditor" / "templates" / "auditor" / "base.html"
        ).read_text(encoding="utf-8")

        self.assertIn("h2 { text-transform: capitalize; }", arkusz)
        # Dokładna reguła, nie samo wystąpienie nazwy klasy: ta sama nazwa pojawia
        # się w arkuszu także w innych regułach, więc luźne szukanie przeszłoby
        # również wtedy, gdyby wyłączenia kapitalizacji w ogóle nie było.
        reguła = (
            ".analytics-connect-title,\n"
            "        .analytics-empty-title { text-transform: none; }"
        )
        self.assertIn(reguła, arkusz)


class AutoMatchClaimTests(ConnectFormBase):
    """Dymek o automatycznym dopasowaniu Search Console."""

    def test_claim_appears_only_with_a_real_match(self):
        html = self._html(witryny=[WITRYNA_PASUJACA])

        self.assertIn("dopasowano <strong>sc-domain:earlystage.pl</strong>", html)

    def test_claim_names_the_actual_site_not_the_domain(self):
        # „Dopasowane do earlystage.pl" nie mówiło, CO zostało przypisane.
        html = self._html(witryny=[WITRYNA_PASUJACA])

        self.assertIn("sc-domain:earlystage.pl", html)

    def test_no_claim_when_the_account_lacks_the_domain(self):
        # Sedno błędu: po przełączeniu konta formularz nadal obiecywał dopasowanie.
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertNotIn("dopasowano <strong>", html)

    def test_missing_access_is_stated_plainly(self):
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn(
            "nie ma dostępu do witryny <strong>earlystage.pl</strong>", html
        )
        self.assertIn("przełącz konto Google", html)

    def test_missing_access_looks_like_a_warning(self):
        # Szary dymek czytałby się jak potwierdzenie, że wszystko w porządku.
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn("analytics-auto-gsc-warning", html)

    def test_matching_site_is_enough_even_without_a_matching_property(self):
        # Dostęp do GA4 i do Search Console nadaje się niezależnie.
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_PASUJACA])

        self.assertIn("dopasowano <strong>sc-domain:earlystage.pl</strong>", html)


class SiteSelectorTests(ConnectFormBase):
    """Lista witryn Search Console, gdy dopasowania nie ma."""

    def test_selector_appears_instead_of_the_false_claim(self):
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn('name="gsc_site_url"', html)
        self.assertIn("sc-domain:innafirma.pl", html)

    def test_selector_allows_assigning_nothing(self):
        # „Nie przypisuj" jest poprawnym wyborem: analityka GA4 działa bez GSC.
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn("nie przypisuj witryny", html)

    def test_no_selector_when_the_match_exists(self):
        # Przy trafionym dopasowaniu lista byłaby zaproszeniem do pomyłki.
        html = self._html(witryny=[WITRYNA_PASUJACA])

        self.assertNotIn('name="gsc_site_url"', html)

    def test_empty_account_says_there_is_nothing_to_choose(self):
        html = self._html()

        self.assertIn("nie ma dostępu do żadnej witryny", html)
        self.assertNotIn('name="gsc_site_url"', html)

    def test_chosen_site_is_accepted_by_the_save(self):
        # Pole musi faktycznie działać, nie tylko się wyświetlać.
        with patch("auditor.views._has_access_to_property", return_value=True), patch(
            "auditor.views._verified_gsc_site",
            return_value="sc-domain:innafirma.pl",
        ), patch("auditor.views.apply_google_services") as zapis:
            zapis.return_value = MagicMock(changed=True, fetched=True)
            self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {
                    "ga4_property_id": "777777",
                    "gsc_site_url": "sc-domain:innafirma.pl",
                    "next": "analytics",
                },
            )

        self.assertEqual(zapis.call_args.kwargs["site_url"], "sc-domain:innafirma.pl")


class NoMatchExitTests(ConnectFormBase):
    """Wyjście dla konta, które nie dotyczy tej domeny."""

    def test_link_appears_when_nothing_matches(self):
        # Konto agencyjne ma zwykle kilkanaście usług, tylko wszystkie dotyczą
        # innych firm - wtedy przeglądanie listy nie pomoże.
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn("Przejdź do analizy domen dostępnych na tym koncie", html)
        self.assertIn(f'href="{reverse("auditor:analytics")}"', html)

    def test_link_explains_that_the_account_sees_other_projects(self):
        html = self._html(wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA])

        self.assertIn("konto widzi inne projekty", html)

    def test_empty_account_gets_the_link_too(self):
        html = self._html()

        self.assertIn("Przejdź do analizy domen dostępnych na tym koncie", html)
        self.assertIn("ani do usługi Analytics 4, ani do witryny", html)

    def test_no_link_when_something_matches(self):
        # Jest co wybrać dla tej domeny - odsyłanie gdzie indziej byłoby mylące.
        html = self._html(wlasciwosci=[GA4_PASUJACA], witryny=[WITRYNA_PASUJACA])

        self.assertNotIn("Przejdź do analizy domen dostępnych na tym koncie", html)

    def test_matching_property_alone_suppresses_the_link(self):
        html = self._html(wlasciwosci=[GA4_PASUJACA], witryny=[WITRYNA_OBCA])

        self.assertNotIn("Przejdź do analizy domen dostępnych na tym koncie", html)


class SelectorFlagTests(ConnectFormBase):
    """Flagi, na których opiera się szablon."""

    def test_matched_site_reaches_the_context(self):
        kontekst = self._open(witryny=[WITRYNA_PASUJACA]).context

        self.assertEqual(
            kontekst["selectors"]["gsc_matched"], "sc-domain:earlystage.pl"
        )
        self.assertTrue(kontekst["selectors"]["has_match"])

    def test_foreign_account_has_no_match(self):
        kontekst = self._open(
            wlasciwosci=[GA4_OBCA], witryny=[WITRYNA_OBCA]
        ).context

        self.assertEqual(kontekst["selectors"]["gsc_matched"], "")
        self.assertFalse(kontekst["selectors"]["has_match"])
        # Usługi są, tylko nie te - to różnica, od której zależy treść komunikatu.
        self.assertTrue(kontekst["selectors"]["has_any_option"])

    def test_subdomain_counts_as_a_match(self):
        # Jedno konto zwykle ma dostęp do całej rodziny adresów jednej firmy.
        kontekst = self._open(
            witryny=[
                {
                    "site_url": "https://materialy.earlystage.pl/",
                    "label": "materialy.earlystage.pl",
                    "domain": "materialy.earlystage.pl",
                }
            ]
        ).context

        self.assertTrue(kontekst["selectors"]["has_match"])

    def test_empty_account_has_neither_match_nor_options(self):
        kontekst = self._open().context

        self.assertFalse(kontekst["selectors"]["has_match"])
        self.assertFalse(kontekst["selectors"]["has_any_option"])
