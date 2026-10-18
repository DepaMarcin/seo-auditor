"""Spójność domen przy przypisywaniu usług Google.

Dla audytu `amso.eu` dało się wybrać usługę GA4 `enova.pl` i aplikacja zapisywała to
bez słowa, mieszając metryki jednej firmy z frazami Search Console drugiej. Takiego
raportu NIE DA SIĘ rozpoznać jako błędnego: wszystkie liczby są prawdziwe, tylko
opisują dwie różne witryny. Dlatego odmowa zapisu, a nie ostrzeżenie obok.

Żadne wywołanie Google nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.models import Audit, GoogleAccount
from auditor.services.google_api import same_site

User = get_user_model()

USLUGI_GA4 = [
    {"property_id": "111", "display_name": "enova.pl", "account_name": "Agencja"},
    {"property_id": "222", "display_name": "amso.eu - GA4", "account_name": "Agencja"},
    {"property_id": "333", "display_name": "Klient bez domeny", "account_name": "Agencja"},
    {"property_id": "444", "display_name": "sklep.amso.eu", "account_name": "Agencja"},
]
WITRYNY_GSC = [
    {"site_url": "sc-domain:enova.pl", "label": "sc-domain:enova.pl", "domain": "enova.pl"},
    {"site_url": "sc-domain:amso.eu", "label": "sc-domain:amso.eu", "domain": "amso.eu"},
    {"site_url": "https://amso.eu/", "label": "https://amso.eu/", "domain": "amso.eu"},
]


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów, ze zwiniętymi białymi znakami."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


class SameSiteTests(SimpleTestCase):
    """`same_site` - reguła "ta sama witryna"."""

    def test_identical_domains_match(self):
        self.assertTrue(same_site("amso.eu", "amso.eu"))

    def test_notation_does_not_matter(self):
        for zapis in ("https://amso.eu/", "http://amso.eu", "www.amso.eu",
                      "sc-domain:amso.eu", "amso.eu/kontakt"):
            with self.subTest(zapis=zapis):
                self.assertTrue(same_site(zapis, "amso.eu"))

    def test_subdomain_matches_both_ways(self):
        # Usługa subdomeny obsługuje audyt domeny głównej i odwrotnie.
        self.assertTrue(same_site("sklep.amso.eu", "amso.eu"))
        self.assertTrue(same_site("amso.eu", "sklep.amso.eu"))

    def test_different_companies_do_not_match(self):
        self.assertFalse(same_site("enova.pl", "amso.eu"))

    def test_different_tld_does_not_match(self):
        # Ta sama nazwa pod innym rozszerzeniem to inna witryna w Google.
        self.assertFalse(same_site("amso.pl", "amso.eu"))

    def test_similar_suffix_is_not_a_subdomain(self):
        # "notamso.eu" kończy się na "amso.eu", ale nie jest jego subdomeną.
        self.assertFalse(same_site("notamso.eu", "amso.eu"))

    def test_empty_is_not_a_match(self):
        # "Nie wiem" to nie "to samo".
        self.assertFalse(same_site("", "amso.eu"))
        self.assertFalse(same_site("amso.eu", ""))
        self.assertFalse(same_site(None, None))


class AssignmentBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="spojnosc@przyklad.pl",
            email="spojnosc@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)
        konto, _ = GoogleAccount.objects.get_or_create(user=self.user)
        konto.refresh_token_encrypted = "jawny-token-testowy"
        konto.email = "agencja@gmail.com"
        konto.save()
        self.audit = Audit.objects.create(
            url="https://amso.eu/", owner=self.user, status=Audit.Status.COMPLETED
        )

    def _assign(self, **dane):
        """Wysyła formularz przypisania z atrapami wywołań Google."""
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.list_ga4_properties", return_value=USLUGI_GA4
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=WITRYNY_GSC
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url",
            return_value="sc-domain:amso.eu",
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


class MismatchRejectionTests(AssignmentBase):
    """Odmowa zapisu przy niezgodnej domenie."""

    def test_foreign_ga4_property_is_rejected(self):
        # Scenariusz ze zgłoszenia: GA4 enova.pl dla audytu amso.eu.
        komunikaty = self._assign(ga4_property_id="111")

        self.assertTrue(
            any("nie pasuje do badanej domeny" in m for m in komunikaty), komunikaty
        )
        self.assertIsNone(self.audit.ga4_property_id)

    def test_rejection_names_both_domains(self):
        komunikaty = self._assign(ga4_property_id="111")

        powod = next(m for m in komunikaty if "nie pasuje" in m)
        self.assertIn("enova.pl", powod)
        self.assertIn("amso.eu", powod)

    def test_rejection_does_not_overwrite_existing_configuration(self):
        # Odmowa musi zostawić poprawną konfigurację nietkniętą - inaczej pomyłka
        # kasowałaby działające przypisanie.
        Audit.objects.filter(pk=self.audit.pk).update(
            ga4_property_id="222", gsc_site_url="sc-domain:amso.eu"
        )

        self._assign(ga4_property_id="111")

        self.assertEqual(self.audit.ga4_property_id, "222")
        self.assertEqual(self.audit.gsc_site_url, "sc-domain:amso.eu")

    def test_foreign_gsc_site_is_rejected(self):
        komunikaty = self._assign(
            ga4_property_id="222", gsc_site_url="sc-domain:enova.pl"
        )

        self.assertTrue(any("nie pasuje do badanej domeny" in m for m in komunikaty))
        self.assertFalse(self.audit.gsc_site_url)

    def test_nothing_is_fetched_when_the_assignment_is_refused(self):
        # Pobranie danych odrzuconej usługi byłoby i kosztem, i śmieciem w bazie.
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.list_ga4_properties", return_value=USLUGI_GA4
        ), patch(
            "auditor.views.apply_google_services",
            side_effect=AssertionError("nie powinno dojść do zapisu"),
        ):
            self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {"ga4_property_id": "111", "next": "analytics"},
            )


class MatchAcceptanceTests(AssignmentBase):
    """Co musi przejść walidację."""

    def test_matching_property_is_accepted(self):
        komunikaty = self._assign(ga4_property_id="222")

        self.assertFalse(any("nie pasuje" in m for m in komunikaty), komunikaty)
        self.assertEqual(self.audit.ga4_property_id, "222")

    def test_subdomain_property_is_accepted(self):
        # Wyjątek wprost z zadania: sklep.amso.eu dla audytu amso.eu.
        komunikaty = self._assign(ga4_property_id="444")

        self.assertFalse(any("nie pasuje" in m for m in komunikaty), komunikaty)
        self.assertEqual(self.audit.ga4_property_id, "444")

    def test_property_without_a_domain_in_its_name_is_accepted(self):
        # Nazwa usługi to dowolny tekst ("Klient bez domeny"). Odmowa na podstawie
        # braku domeny w nazwie uniemożliwiłaby przypisanie takiej usługi NA ZAWSZE,
        # a nie mamy podstaw twierdzić, że jest niewłaściwa.
        komunikaty = self._assign(ga4_property_id="333")

        self.assertFalse(any("nie pasuje" in m for m in komunikaty), komunikaty)
        self.assertEqual(self.audit.ga4_property_id, "333")

    def test_matching_gsc_site_is_accepted(self):
        komunikaty = self._assign(
            ga4_property_id="222", gsc_site_url="sc-domain:amso.eu"
        )

        self.assertFalse(any("nie pasuje" in m for m in komunikaty), komunikaty)

    def test_clearing_the_property_is_not_a_mismatch(self):
        # Puste pole to świadomy wybór "brak", nie niezgodna domena.
        komunikaty = self._assign(ga4_property_id="")

        self.assertFalse(any("nie pasuje" in m for m in komunikaty), komunikaty)

    def test_unreachable_google_does_not_block_the_save(self):
        # Bez listy usług nie wiemy, jaka jest domena wybranej - a odmowa przy
        # awarii Google uniemożliwiałaby pracę.
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.list_ga4_properties", return_value=[]
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[]
        ), patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data"
        ), patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            self.client.post(
                reverse("auditor:assign_google_services", args=[self.audit.pk]),
                {"ga4_property_id": "111", "next": "analytics"},
            )

        self.audit.refresh_from_db()
        self.assertEqual(self.audit.ga4_property_id, "111")


class PropertyListSplitTests(AssignmentBase):
    """Podział listy usług GA4 na pasujące i pozostałe."""

    def _html(self):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="agencja@gmail.com",
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=USLUGI_GA4,
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=WITRYNY_GSC
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            odpowiedz = self.client.get(
                reverse("auditor:audit_analytics", args=[self.audit.pk])
            )
        self.kontekst = odpowiedz.context
        return _body_only(odpowiedz.content.decode())

    def test_both_groups_are_rendered(self):
        html = self._html()

        self.assertIn('<optgroup label="Usługi pasujące do amso.eu">', html)
        self.assertIn('<optgroup label="Pozostałe usługi na koncie (inne domeny)">', html)

    def test_matching_property_is_preselected(self):
        # Zamiast pustego "— wybierz usługę —", które zapraszało do pomyłki.
        html = self._html()

        self.assertRegex(html, r'value="222"\s*selected')

    def test_foreign_property_lands_in_the_other_group(self):
        html = self._html()

        self.assertGreater(
            html.find('value="111"'), html.find("Pozostałe usługi na koncie")
        )

    def test_property_without_a_domain_is_not_called_matching(self):
        # "Klient bez domeny" nie pasuje dowodnie - nie udajemy, że pasuje.
        self._html()

        pasujace = [o["value"] for o in self.kontekst["selectors"]["ga4_matching"]]
        self.assertNotIn("333", pasujace)

    def test_subdomain_property_counts_as_matching(self):
        self._html()

        pasujace = [o["value"] for o in self.kontekst["selectors"]["ga4_matching"]]
        self.assertIn("444", pasujace)

    def test_groups_split_every_property(self):
        self._html()

        selektory = self.kontekst["selectors"]
        razem = len(selektory["ga4_matching"]) + len(selektory["ga4_other"])
        self.assertEqual(razem, len(USLUGI_GA4))


class GscChoiceTests(AssignmentBase):
    """Witryny Search Console: jedna, kilka albo żadna."""

    def _html(self, witryny):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="agencja@gmail.com",
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=witryny
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            return _body_only(
                self.client.get(
                    reverse("auditor:audit_analytics", args=[self.audit.pk])
                ).content.decode()
            )

    def test_single_match_is_assigned_without_asking(self):
        html = self._html([WITRYNY_GSC[1]])

        self.assertIn("dopasowano <strong>sc-domain:amso.eu</strong>", html)
        self.assertNotIn('name="gsc_site_url"', html)

    def test_several_matches_are_offered_as_a_choice(self):
        # Search Console trzyma zwykle tę samą witrynę jako usługę domenową
        # I prefiksową - kilka trafień to norma, nie wyjątek.
        html = self._html(WITRYNY_GSC)

        self.assertIn('name="gsc_site_url"', html)
        self.assertIn("Kilka witryn tego konta pasuje do domeny amso.eu", html)

    def test_choice_lists_only_matching_sites(self):
        # Witryna innej domeny zostanie odrzucona przy zapisie, więc pokazywanie
        # jej byłoby zaproszeniem do błędu, który i tak nie przejdzie.
        html = self._html(WITRYNY_GSC)

        pole = html[html.find('name="gsc_site_url"'):]
        pole = pole[: pole.find("</select>")]
        self.assertNotIn("enova.pl", pole)

    def test_no_match_offers_no_selector(self):
        html = self._html([WITRYNY_GSC[0]])

        self.assertNotIn('name="gsc_site_url"', html)
        self.assertIn("nie ma dostępu do witryny", html)


class BubbleLayoutTests(AssignmentBase):
    """Układ dymku o dopasowaniu Search Console."""

    def test_message_is_one_flex_item(self):
        # Wcześniej akapit zawierał ikonę, tekst, <strong> i kolejny tekst - cztery
        # elementy flex, które przy wąskiej karcie ściskały się w pionowe kolumny.
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_api.fetch_account_email",
            return_value="agencja@gmail.com",
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[],
        ), patch(
            "auditor.services.google_api.list_gsc_sites", return_value=[WITRYNY_GSC[1]]
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.get_available_events",
            return_value=[],
        ):
            html = _body_only(
                self.client.get(
                    reverse("auditor:audit_analytics", args=[self.audit.pk])
                ).content.decode()
            )

        self.assertIn('class="analytics-auto-gsc-text"', html)

    def test_container_wraps_instead_of_squeezing(self):
        from pathlib import Path

        from django.conf import settings

        arkusz = (
            Path(settings.BASE_DIR) / "auditor" / "templates" / "auditor" / "base.html"
        ).read_text(encoding="utf-8")
        blok = arkusz.split(".analytics-auto-gsc {")[1].split("}")[0]

        self.assertIn("flex-wrap: wrap", blok)
        self.assertIn("align-items: center", blok)
