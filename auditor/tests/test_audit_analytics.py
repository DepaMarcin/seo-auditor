"""Analityka pojedynczego audytu - dwa stany, bez listy innych projektów.

Wcześniej zakładka "Analityka i Ruch" była globalną tablicą wszystkich audytów wraz
z ich stanami połączenia. Dane GA4 i GSC opisują zawsze jedną domenę, więc powrót
z autoryzacji Google na taką listę gubił kontekst pracy nad konkretnym projektem.
"""
from __future__ import annotations

import re

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów - nazwa klasy w CSS nie znaczy, że element istnieje."""
    import re

    return re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL)

User = get_user_model()


def _without_google():
    """Audyt bez połączenia - żadne wywołanie Google nie powinno się wydarzyć."""
    return patch(
        "auditor.services.google_services.build_credentials_from_refresh_token",
        side_effect=AssertionError("nie powinno być odpytywane bez tokenu"),
    )


class AnalyticsBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="analityka@przyklad.pl",
            email="analityka@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.orlen = Audit.objects.create(url="https://orlen.pl/", owner=cls.user)
        cls.shell = Audit.objects.create(url="https://shell.pl/", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()

    def _audit_with_token(self, audit: Audit) -> Audit:
        """Audyt z poświadczeniami, ale bez przypisanej usługi."""
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )
        return Audit.objects.get(pk=audit.pk)

    def _connect(self, audit: Audit, property_id="222222") -> Audit:
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy",
            ga4_property_id=property_id,
            ga4_organic_sessions=4321,
            gsc_total_clicks_current=890,
            gsc_total_clicks_previous=700,
        )
        return Audit.objects.get(pk=audit.pk)

    def _open(self, audit: Audit):
        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=[{"property_id": "222222", "display_name": "shell.pl", "account_name": "Konto"}],
        ), patch("auditor.services.google_api.list_gsc_sites", return_value=[]):
            return self.client.get(reverse("auditor:audit_analytics", args=[audit.pk]))


class EmptyStateTests(AnalyticsBase):
    """Stan 1: nic nie jest podłączone."""

    def test_page_names_the_domain(self):
        response = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak podłączonej analityki dla domeny")
        self.assertContains(response, "shell.pl")

    def test_connect_button_points_at_this_audit(self):
        html = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        ).content.decode()

        self.assertIn(f'href="/audits/{self.shell.pk}/ga4/connect/', html)
        self.assertIn("Zaloguj przez Google", html)

    def test_other_domains_appear_only_in_the_switcher(self):
        # Przełącznik domeny z definicji wymienia pozostałe audyty. Poza nim treść
        # ekranu dotyczy wyłącznie bieżącej domeny.
        html = _body_only(
            self.client.get(
                reverse("auditor:audit_analytics", args=[self.shell.pk])
            ).content.decode()
        )

        przelacznik = html.split('class="analytics-domain-switch"', 1)
        poza_przelacznikiem = przelacznik[0] + przelacznik[1].split("</form>", 1)[1]

        self.assertNotIn("orlen.pl", poza_przelacznikiem)

    def test_no_charts_or_numbers_in_the_empty_state(self):
        html = _body_only(
            self.client.get(
                reverse("auditor:audit_analytics", args=[self.shell.pk])
            ).content.decode()
        )

        self.assertNotIn("analytics-big-number", html)
        self.assertNotIn("Zmień usługę Google", html)

    def test_connected_account_without_property_asks_only_for_the_service(self):
        # Konto jest wspólne; brakuje wyłącznie wskazania usługi dla tej domeny.
        Audit.objects.filter(pk=self.shell.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        response = self._open(Audit.objects.get(pk=self.shell.pk))

        self.assertContains(response, "Wskaż usługi dla domeny")
        self.assertContains(response, "Podłącz i pobierz dane")
        self.assertContains(response, "jan@gmail.com")

    def test_state_is_driven_by_the_property_not_the_token(self):
        Audit.objects.filter(pk=self.shell.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )

        response = self._open(Audit.objects.get(pk=self.shell.pk))

        self.assertFalse(response.context["has_analytics"])


class DataStateTests(AnalyticsBase):
    """Stan 2: usługa przypisana."""

    def test_numbers_are_shown(self):
        audit = self._connect(self.shell)

        response = self._open(audit)

        self.assertTrue(response.context["has_analytics"])
        self.assertContains(response, "4321")
        self.assertContains(response, "890")

    def test_assigned_services_are_visible(self):
        audit = self._connect(self.shell)

        response = self._open(audit)

        self.assertContains(response, "222222")

    def test_reconfiguration_link_is_discreet_but_present(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertIn("⚙️ Zmień usługę / domenę", html)
        self.assertIn(f'action="/analytics/{audit.pk}/assign/"', html)

    def test_empty_state_banner_is_gone(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertNotIn("Brak podłączonej analityki", html)

    def test_data_section_describes_only_the_current_domain(self):
        self._connect(self.shell)
        self._connect(self.orlen, property_id="111111")

        html = _body_only(self._open(Audit.objects.get(pk=self.shell.pk)).content.decode())
        czesci = html.split('class="analytics-domain-switch"', 1)
        poza_przelacznikiem = czesci[0] + czesci[1].split("</form>", 1)[1]

        self.assertNotIn("orlen.pl", poza_przelacznikiem)
        self.assertNotIn("111111", poza_przelacznikiem)

    def test_link_back_to_the_report(self):
        audit = self._connect(self.shell)

        html = self._open(audit).content.decode()

        self.assertIn(f'href="/audits/{audit.pk}/"', html)


class IsolationTests(AnalyticsBase):
    """Cudza analityka jest niedostępna."""

    def test_foreign_audit_returns_404(self):
        obcy = User.objects.create_user(
            username="obcy@przyklad.pl",
            email="obcy@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        response = self.client.get(
            reverse("auditor:audit_analytics", args=[cudzy.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_login_required(self):
        self.client.logout()

        response = self.client.get(
            reverse("auditor:audit_analytics", args=[self.shell.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class EntryRequiresExplicitChoiceTests(AnalyticsBase):
    """`/analytics/` nie wybiera projektu za użytkownika.

    Wcześniej brał najnowszy audyt. Przy kilkudziesięciu audytach to loteria:
    użytkownik konfigurował usługę Google dla domeny, której nie zauważył
    w nagłówku, i witryna jednego klienta trafiała do audytu drugiego.
    """

    def test_entry_asks_which_project(self):
        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auditor/analytics_pick.html")
        self.assertContains(response, "Wybierz projekt")

    def test_entry_does_not_render_any_data(self):
        # Sedno objawu: dashboard z liczbami domeny, której nikt nie wskazał.
        self._connect(self.shell)

        html = _body_only(self.client.get(reverse("auditor:analytics")).content.decode())

        self.assertNotIn("analytics-big-number", html)
        self.assertNotIn("4321", html)

    def test_entry_offers_every_own_audit(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        # Każdy projekt to wiersz z własnym przyciskiem akcji. Liczymy przyciski,
        # a nie wystąpienia prefiksu klasy - wiersz zawiera kilka klas `project-row-*`.
        self.assertIn("shell.pl", html)
        self.assertIn("orlen.pl", html)
        self.assertEqual(len(re.findall(r'class="project-row-button', html)), 2)

    def test_chosen_project_is_shown(self):
        response = self.client.get(reverse("auditor:analytics"), {"audit": self.orlen.pk})

        self.assertEqual(response.context["audit"], self.orlen)
        self.assertTemplateUsed(response, "auditor/analytics_dashboard.html")

    def test_single_audit_needs_no_choosing(self):
        Audit.objects.filter(pk=self.orlen.pk).delete()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertTemplateUsed(response, "auditor/analytics_dashboard.html")
        self.assertEqual(response.context["audit"], self.shell)

    def test_foreign_audit_parameter_asks_again(self):
        obcy = User.objects.create_user(
            username="obcy3@przyklad.pl",
            email="obcy3@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        cudzy = Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        response = self.client.get(reverse("auditor:analytics"), {"audit": cudzy.pk})

        self.assertTemplateUsed(response, "auditor/analytics_pick.html")
        self.assertNotContains(response, "cudzy.pl")

    def test_without_any_audit_user_is_sent_to_the_scanner(self):
        Audit.objects.all().delete()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertRedirects(
            response, reverse("auditor:index"), fetch_redirect_response=False
        )

    def test_entry_requires_login(self):
        self.client.logout()

        response = self.client.get(reverse("auditor:analytics"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


HEADER_RE = r'class="geo-header-subtitle">\s*<strong>([^<]+)</strong>'


class HeaderMatchesAssignedServiceTests(AnalyticsBase):
    """Nagłówek opisuje dokładnie ten audyt, którego dane widać."""

    def test_header_domain_equals_the_audit_in_context(self):
        audit = self._connect(self.shell)

        response = self._open(audit)
        naglowek = re.search(HEADER_RE, response.content.decode())

        self.assertIsNotNone(naglowek, "nie znaleziono domeny w nagłówku")
        self.assertEqual(naglowek.group(1), response.context["audit"].url)
        self.assertEqual(naglowek.group(1), audit.url)

    def test_header_does_not_show_another_audit(self):
        self._connect(self.shell)
        self._connect(self.orlen, property_id="111111")

        html = self._open(Audit.objects.get(pk=self.shell.pk)).content.decode()
        naglowek = re.search(HEADER_RE, html).group(1)

        self.assertIn("shell.pl", naglowek)
        self.assertNotIn("orlen.pl", naglowek)

    def test_assigned_property_belongs_to_the_shown_audit(self):
        audit = self._connect(self.shell, property_id="222222")

        response = self._open(audit)

        self.assertEqual(response.context["audit"].ga4_property_id, "222222")
        self.assertContains(response, "222222")

    def test_mismatched_search_console_site_is_flagged(self):
        # Dokładnie przypadek z bazy: audyt orlen.pl z witryną enova.pl.
        audit = self._connect(self.shell)
        Audit.objects.filter(pk=audit.pk).update(gsc_site_url="https://orlen.pl/")

        response = self._open(Audit.objects.get(pk=audit.pk))

        self.assertTrue(response.context["mismatched_site"])
        self.assertContains(response, "Niespójna konfiguracja")

    def test_matching_site_is_not_flagged(self):
        audit = self._connect(self.shell)
        Audit.objects.filter(pk=audit.pk).update(gsc_site_url="sc-domain:shell.pl")

        response = self._open(Audit.objects.get(pk=audit.pk))

        self.assertFalse(response.context["mismatched_site"])
        self.assertNotContains(response, "Niespójna konfiguracja")


class NoMessageSpamTests(AnalyticsBase):
    """Powtarzane zapisy nie zostawiają kolejki powiadomień."""

    def _save(self, audit, property_id=None, site_url=None):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data"
        ):
            return self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {
                    "ga4_property_id": (
                        audit.ga4_property_id or "" if property_id is None else property_id
                    ),
                    "gsc_site_url": (
                        audit.gsc_site_url or "" if site_url is None else site_url
                    ),
                    "next": "analytics",
                },
                follow=bool(property_id or site_url),
            )

    def test_unchanged_save_produces_no_message(self):
        audit = self._connect(self.shell)

        self._save(audit)
        response = self._open(audit)

        self.assertEqual(list(response.context["messages"]), [])

    def test_seven_unchanged_saves_produce_no_queue(self):
        audit = self._connect(self.shell)

        for _ in range(7):
            self._save(audit)

        response = self._open(audit)

        self.assertEqual(len(list(response.context["messages"])), 0)

    def test_real_change_reports_once(self):
        audit = self._connect(self.shell, property_id="999999")

        response = self._save(audit, property_id="222222", site_url="")

        komunikaty = [m.message for m in response.context["messages"]]
        self.assertEqual(len(komunikaty), 1, "komunikaty: %s" % komunikaty)


class SelectorSuggestionTests(AnalyticsBase):
    """Selektory w karcie konfiguracji podpowiadają usługę pasującą do domeny.

    Te przypadki pokrywały wcześniej testy globalnego panelu - ich przedmiot
    przeniósł się razem z selektorami do widoku pojedynczego audytu.
    """

    def _open_with(self, audit, properties, sites):
        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.google_api.fetch_account_email", return_value="jan@gmail.com"
        ), patch(
            "auditor.services.ga4_service.GA4OAuthService.list_accessible_properties",
            return_value=properties,
        ), patch("auditor.services.google_api.list_gsc_sites", return_value=sites):
            return self.client.get(reverse("auditor:audit_analytics", args=[audit.pk]))

    def test_matching_property_is_suggested_first(self):
        audit = self._connect(self.shell)

        response = self._open_with(
            audit,
            [
                {"property_id": "999", "display_name": "zupelnie inna", "account_name": "Konto"},
                {"property_id": "222222", "display_name": "shell.pl - GA4", "account_name": "Konto"},
            ],
            [],
        )

        opcje = response.context["selectors"]["ga4_options"]
        self.assertTrue(opcje[0]["suggested"])
        self.assertEqual(opcje[0]["value"], "222222")

    def test_matching_gsc_site_is_suggested_first(self):
        audit = self._connect(self.shell)

        response = self._open_with(
            audit,
            [],
            [
                {"site_url": "https://obca.pl/", "label": "https://obca.pl/", "domain": "obca.pl"},
                {"site_url": "sc-domain:shell.pl", "label": "shell.pl (usługa domenowa)", "domain": "shell.pl"},
            ],
        )

        opcje = response.context["selectors"]["gsc_options"]
        self.assertTrue(opcje[0]["suggested"])
        self.assertEqual(opcje[0]["value"], "sc-domain:shell.pl")

    def test_suggestion_is_labelled_in_the_markup(self):
        audit = self._connect(self.shell)

        html = self._open_with(
            audit,
            [{"property_id": "222222", "display_name": "shell.pl - GA4", "account_name": "Agencja"}],
            [],
        ).content.decode()

        self.assertIn("(Sugerowana)", html)
        self.assertIn("Agencja", html)

    def test_account_email_is_cached_on_the_audit(self):
        audit = self._connect(self.shell)

        self._open_with(audit, [], [])

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_account_email, "jan@gmail.com")

    def test_google_outage_does_not_break_the_page(self):
        audit = self._connect(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token",
            side_effect=Exception("brak client_secret"),
        ):
            response = self.client.get(
                reverse("auditor:audit_analytics", args=[audit.pk])
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["google"]["connected"])

    def test_unreadable_token_is_treated_as_no_connection(self):
        audit = self._connect(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=None
        ):
            response = self.client.get(
                reverse("auditor:audit_analytics", args=[audit.pk])
            )

        self.assertFalse(response.context["google"]["connected"])
        self.assertEqual(response.context["google"]["error"], "token")


class AutomaticGscBindingTests(AnalyticsBase):
    """Witryna Search Console wiąże się sama z domeną audytu.

    Ręczny wybór okazał się źródłem przypisań w poprzek projektów: witryna jednego
    klienta trafiała do audytu drugiego i raport pokazywał cudze liczby pod właściwą
    nazwą domeny.
    """

    def _assign(self, audit, property_id="222222", matched="sc-domain:shell.pl"):
        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url", return_value=matched
        ) as resolve, patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data"
        ), patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            response = self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": property_id, "next": "analytics"},
            )
        return response, resolve

    def test_site_is_matched_to_the_audit_domain(self):
        audit = self._audit_with_token(self.shell)

        self._assign(audit)

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_site_url, "sc-domain:shell.pl")

    def test_matching_uses_the_audit_url(self):
        audit = self._audit_with_token(self.shell)

        _, resolve = self._assign(audit)

        # Dopasowanie musi wyjść od adresu TEGO audytu, nie od niczego innego.
        self.assertEqual(resolve.call_args.args[1], audit.url)

    def test_form_cannot_impose_a_foreign_site(self):
        # Nawet podrzucone w POST pole nie przypisze witryny innej domeny.
        audit = self._audit_with_token(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url",
            return_value="sc-domain:shell.pl",
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data"
        ):
            self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {
                    "ga4_property_id": "222222",
                    "gsc_site_url": "https://orlen.pl/",
                    "next": "analytics",
                },
            )

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_site_url, "sc-domain:shell.pl")

    def test_existing_mismatch_is_repaired_on_save(self):
        # Audyt z błędnym przypisaniem z przeszłości wyrównuje się przy zapisie.
        audit = self._audit_with_token(self.shell)
        Audit.objects.filter(pk=audit.pk).update(gsc_site_url="https://orlen.pl/")

        self._assign(Audit.objects.get(pk=audit.pk))

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_site_url, "sc-domain:shell.pl")

    def test_gsc_data_is_refetched_after_repair(self):
        audit = self._audit_with_token(self.shell)
        Audit.objects.filter(pk=audit.pk).update(
            gsc_site_url="https://orlen.pl/", gsc_total_clicks_current=9999
        )

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url",
            return_value="sc-domain:shell.pl",
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data"
        ) as sync_gsc:
            self.client.post(
                reverse("auditor:assign_google_services", args=[Audit.objects.get(pk=audit.pk).pk]),
                {"ga4_property_id": "222222", "next": "analytics"},
            )

        audit.refresh_from_db()
        sync_gsc.assert_called_once()
        # Liczby poprzedniej witryny zniknęły razem z nią.
        self.assertEqual(audit.gsc_total_clicks_current, 0)

    def test_no_matching_site_leaves_the_field_empty(self):
        # Brak dopasowania nie jest błędem: przy pobieraniu GSCService spróbuje znowu.
        audit = self._audit_with_token(self.shell)

        self._assign(audit, matched=None)

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_site_url, "")

    def test_matching_failure_does_not_break_the_save(self):
        audit = self._audit_with_token(self.shell)

        with patch(
            "auditor.views._build_credentials_from_refresh_token", return_value=MagicMock()
        ), patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url",
            side_effect=Exception("Google nie odpowiada"),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data"
        ):
            response = self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {"ga4_property_id": "222222", "next": "analytics"},
            )

        audit.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(audit.ga4_property_id, "222222")
        self.assertEqual(audit.gsc_site_url, "")

    def test_service_layer_accepts_an_explicit_site(self):
        # Panel administracyjny wskazuje witrynę wprost - ta droga zostaje.
        from auditor.services.google_services import apply_google_services

        audit = self._audit_with_token(self.shell)

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data"
        ), patch(
            "auditor.services.gsc_service.GSCService.resolve_site_url"
        ) as resolve:
            apply_google_services(audit, "222222", "sc-domain:wskazana-recznie.pl")

        audit.refresh_from_db()
        resolve.assert_not_called()
        self.assertEqual(audit.gsc_site_url, "sc-domain:wskazana-recznie.pl")


class ProjectPickerTests(AnalyticsBase):
    """Ekran wyboru projektu: każdy wiersz prowadzi tam, gdzie trzeba.

    Trzy stany prowadzą w trzy różne miejsca, więc każdy wiersz ma własny przycisk.
    Wspólny przycisk pod listą musiałby zgadywać, co dla danego projektu znaczy "dalej".
    """

    def _rows(self):
        response = self.client.get(reverse("auditor:analytics"))
        return {row["audit"].pk: row for row in response.context["projects"]}, response

    def test_project_without_connection_offers_to_connect(self):
        wiersze, response = self._rows()

        wiersz = wiersze[self.shell.pk]
        self.assertEqual(wiersz["action"], "connect")
        self.assertEqual(wiersz["label"], "Połącz z GA4 i GSC")
        self.assertContains(response, "Połącz z GA4 i GSC")

    def test_connect_button_leads_straight_to_google(self):
        html = self.client.get(reverse("auditor:analytics")).content.decode()

        # Bez tokenu pierwszym krokiem jest zgoda Google - bez przystanku po drodze.
        self.assertIn(
            f'href="/audits/{self.shell.pk}/ga4/connect/?return_to=analytics"', html
        )

    def test_project_with_account_but_no_service_offers_to_pick_one(self):
        self._audit_with_token(self.shell)

        wiersze, response = self._rows()

        wiersz = wiersze[self.shell.pk]
        self.assertEqual(wiersz["action"], "configure")
        self.assertEqual(wiersz["label"], "Wskaż usługę GA4")
        self.assertContains(response, "Konto Google gotowe")

    def test_configured_project_offers_the_data(self):
        self._connect(self.shell)

        wiersze, response = self._rows()

        wiersz = wiersze[self.shell.pk]
        self.assertEqual(wiersz["action"], "data")
        self.assertEqual(wiersz["label"], "Pokaż analitykę")
        self.assertContains(response, "Analityka podłączona")

    def test_each_row_links_to_its_own_project(self):
        self._connect(self.shell)

        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn(f'?audit={self.shell.pk}"', html)
        self.assertIn(f'/audits/{self.orlen.pk}/ga4/connect/', html)

    def test_rows_show_the_connection_state(self):
        self._connect(self.shell)
        self._audit_with_token(self.orlen)

        html = self.client.get(reverse("auditor:analytics")).content.decode()

        self.assertIn("Analityka podłączona", html)
        self.assertIn("Konto Google gotowe", html)

    def test_no_dropdown_to_pick_from(self):
        # Wybór w liście rozwijanej był krokiem przed kliknięciem - odpadł.
        html = _body_only(self.client.get(reverse("auditor:analytics")).content.decode())

        self.assertNotIn("analytics-pick-audit", html)
        self.assertNotIn("<select", html)

    def test_only_own_projects_are_offered(self):
        obcy = User.objects.create_user(
            username="obcy4@przyklad.pl",
            email="obcy4@przyklad.pl",
            password="haslo-kontrolne-2",
        )
        Audit.objects.create(url="https://cudzy.pl/", owner=obcy)

        wiersze, response = self._rows()

        self.assertEqual(set(wiersze), {self.shell.pk, self.orlen.pk})
        self.assertNotContains(response, "cudzy.pl")
