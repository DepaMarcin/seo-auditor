"""Blokada przypisania usługi z konta, które nie obsługuje badanej domeny.

Scenariusz ze zgłoszenia: audyt `shell.pl`, konto Google z dostępem wyłącznie do
projektu „Early Stage". Panel pokazywał ostrzeżenia, ale przycisk zapisu pozostawał
aktywny, a backend przyjmował obce ID - bo nazwa usługi nie zawiera domeny, więc
nie dało się DOWIEŚĆ niezgodności.

Rozstrzyga drugie pytanie: czy konto ma dla tej domeny cokolwiek. Gdy nie ma nic,
to cokolwiek użytkownik wybrał, na pewno nie opisuje badanej witryny.

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

# Usługa innej firmy, z nazwy której NIE DA SIĘ odczytać domeny - sedno problemu.
USLUGA_OBCA = {
    "property_id": "363353591",
    "display_name": "Early Stage",
    "account_name": "Konto",
}
USLUGA_WLASNA = {
    "property_id": "222222",
    "display_name": "shell.pl - GA4",
    "account_name": "Konto",
}
WITRYNA_WLASNA = {
    "site_url": "sc-domain:shell.pl",
    "label": "sc-domain:shell.pl",
    "domain": "shell.pl",
}


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów, ze zwiniętymi białymi znakami."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


class ForeignAccountBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="obce-konto@przyklad.pl",
            email="obce-konto@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "hero5@gmail.com"
        konto.save()
        self.audit = Audit.objects.create(
            url="https://shell.pl/", owner=self.user, status=Audit.Status.COMPLETED
        )

    def _open(self, wlasciwosci=(), witryny=()):
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

    def _save(self, wlasciwosci, witryny, **dane):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.list_ga4_properties",
            return_value=list(wlasciwosci),
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=list(witryny)
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url", return_value=""
        ), patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data"
        ), patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            odpowiedz = self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {"next": "analytics", **dane},
                follow=True,
            )
        self.audit.refresh_from_db()
        return [str(m) for m in odpowiedz.context["messages"]]


class SubmitBlockedTests(ForeignAccountBase):
    """Przycisk zapisu przy koncie bez dostępu do badanej domeny."""

    def test_submit_is_disabled_without_any_match(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn('class="analytics-connect-submit" disabled', html)

    def test_submit_is_active_when_a_property_matches(self):
        html = self._html(wlasciwosci=[USLUGA_WLASNA])

        self.assertIn('class="analytics-connect-submit">Podłącz', html)

    def test_submit_is_active_when_only_the_site_matches(self):
        # Dostęp do GA4 i Search Console nadaje się niezależnie.
        html = self._html(wlasciwosci=[USLUGA_OBCA], witryny=[WITRYNA_WLASNA])

        self.assertIn('class="analytics-connect-submit">Podłącz', html)

    def test_disabled_button_explains_itself(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn("To konto nie ma usługi ani witryny dla domeny shell.pl", html)

    def test_exits_replace_the_dead_button(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn("analytics-connect-exits", html)
        self.assertIn("Przełącz konto Google", html)

    def test_account_switch_returns_to_analytics(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn(
            reverse("auditor:start_ga4_auth", args=[self.audit.pk]), html
        )
        self.assertIn("switch=1", html)

    def test_exits_are_hidden_when_something_matches(self):
        html = self._html(wlasciwosci=[USLUGA_WLASNA])

        self.assertNotIn("analytics-connect-exits", html)


class ForeignPropertyNotPreselectedTests(ForeignAccountBase):
    """Obca usługa nie może być wybrana za użytkownika."""

    def test_empty_option_is_selected_and_disabled(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn('<option value="" selected disabled>', html)
        self.assertIn("brak usługi pasującej do tej domeny", html)

    def test_foreign_property_carries_no_selected_attribute(self):
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        pole = html[html.find('name="ga4_property_id"'):]
        pole = pole[: pole.find("</select>")]
        obca = pole[pole.find('value="363353591"'):]
        self.assertNotIn("selected", obca[: obca.find(">")])

    def test_matching_property_is_still_preselected(self):
        # Zaostrzenie nie może zabrać podpowiedzi tam, gdzie jest trafna.
        html = self._html(wlasciwosci=[USLUGA_WLASNA])

        self.assertRegex(html, r'value="222222"\s*selected')

    def test_field_has_a_real_identifier(self):
        # Filtr `add` przy łączeniu tekstu z liczbą zwraca PUSTY ciąg, więc `id`
        # znikało, a `<label for>` wskazywał w próżnię.
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn('id="ga4-connect"', html)
        self.assertNotIn('<select name="ga4_property_id" id=""', html)


class BackendRejectionTests(ForeignAccountBase):
    """Odrzucenie zapisu po stronie serwera - niezależnie od tego, co wyśle formularz."""

    def test_foreign_property_is_refused_even_without_a_domain_in_its_name(self):
        # Formularz blokuje przycisk, ale POST można wysłać z pominięciem interfejsu.
        komunikaty = self._save([USLUGA_OBCA], [], ga4_property_id="363353591")

        self.assertTrue(
            any("nie ma dostępu do żadnej usługi" in m for m in komunikaty), komunikaty
        )
        self.assertIsNone(self.audit.ga4_property_id)

    def test_refusal_names_the_domain_and_the_service(self):
        komunikaty = self._save([USLUGA_OBCA], [], ga4_property_id="363353591")

        powod = next(m for m in komunikaty if "nie ma dostępu do żadnej usługi" in m)
        self.assertIn("shell.pl", powod)
        self.assertIn("Early Stage", powod)

    def test_existing_configuration_survives_the_refusal(self):
        Audit.objects.filter(pk=self.audit.pk).update(ga4_property_id="222222")

        self._save([USLUGA_OBCA], [], ga4_property_id="363353591")

        self.assertEqual(self.audit.ga4_property_id, "222222")

    def test_unnamed_property_passes_when_the_account_covers_the_domain(self):
        # Nazwa bez domeny nie dowodzi niczego. Gdy konto MA witrynę tej domeny,
        # usługa jest prawdopodobnie właściwa - odmowa byłaby zgadywaniem.
        bez_domeny = {"property_id": "555", "display_name": "Klient 7", "account_name": "K"}

        komunikaty = self._save(
            [bez_domeny], [WITRYNA_WLASNA], ga4_property_id="555"
        )

        self.assertFalse(
            any("nie ma dostępu do żadnej usługi" in m for m in komunikaty), komunikaty
        )
        self.assertEqual(self.audit.ga4_property_id, "555")

    def test_unreachable_google_does_not_block_the_save(self):
        # Puste listy mogą znaczyć awarię API - odmowa uniemożliwiałaby wtedy pracę.
        komunikaty = self._save([], [], ga4_property_id="363353591")

        self.assertFalse(
            any("nie ma dostępu do żadnej usługi" in m for m in komunikaty), komunikaty
        )
        self.assertEqual(self.audit.ga4_property_id, "363353591")

    def test_empty_choice_is_not_refused(self):
        komunikaty = self._save([USLUGA_OBCA], [], ga4_property_id="")

        self.assertFalse(
            any("nie ma dostępu do żadnej usługi" in m for m in komunikaty), komunikaty
        )


class OtherDomainExitTests(ForeignAccountBase):
    """Przejście do audytu domeny, którą to konto faktycznie obsługuje."""

    def test_link_points_at_the_audit_of_the_covered_domain(self):
        inny = Audit.objects.create(
            url="https://earlystage.pl/", owner=self.user,
            status=Audit.Status.COMPLETED,
        )

        html = self._html(
            wlasciwosci=[
                {"property_id": "999", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ]
        )

        self.assertIn("Przejdź do audytu domeny earlystage.pl", html)
        self.assertIn(reverse("auditor:audit_analytics", args=[inny.pk]), html)

    def test_without_such_an_audit_the_general_entry_is_offered(self):
        html = self._html(
            wlasciwosci=[
                {"property_id": "999", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ]
        )

        self.assertIn("Przejdź do domen dostępnych na tym koncie", html)
        self.assertNotIn("Przejdź do audytu domeny", html)

    def test_account_without_a_readable_domain_offers_the_general_entry(self):
        # "Early Stage" nie niesie domeny - nie ma dokąd prowadzić wprost.
        html = self._html(wlasciwosci=[USLUGA_OBCA])

        self.assertIn("Przejdź do domen dostępnych na tym koncie", html)

    def test_several_foreign_domains_are_not_guessed(self):
        # Przy kilku obcych domenach nie wiadomo, która jest tą właściwą.
        Audit.objects.create(url="https://earlystage.pl/", owner=self.user)
        Audit.objects.create(url="https://enova.pl/", owner=self.user)

        html = self._html(
            wlasciwosci=[
                {"property_id": "1", "display_name": "earlystage.pl", "account_name": "K"},
                {"property_id": "2", "display_name": "enova.pl", "account_name": "K"},
            ]
        )

        self.assertNotIn("Przejdź do audytu domeny", html)
        self.assertIn("Przejdź do domen dostępnych na tym koncie", html)

    def test_other_users_audit_is_not_offered(self):
        obcy = User.objects.create_user(
            username="obcy61@przyklad.pl",
            email="obcy61@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        Audit.objects.create(url="https://earlystage.pl/", owner=obcy)

        html = self._html(
            wlasciwosci=[
                {"property_id": "999", "display_name": "earlystage.pl",
                 "account_name": "Konto"}
            ]
        )

        self.assertNotIn("Przejdź do audytu domeny", html)
