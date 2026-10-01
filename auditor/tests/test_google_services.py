"""Zunifikowane przypisanie usług Google.

Przypisanie usługi odbywa się dwiema drogami: zaraz po autoryzacji OAuth (ekran wyboru
usługi) i później z panelu analityki. Wcześniej były to dwa osobne widoki, które robiły
prawie to samo - "prawie", bo tylko jeden czyścił dane poprzedniej usługi. Te testy
pilnują, żeby obie drogi kończyły się dokładnie tym samym stanem bazy.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from auditor.models import Audit
from auditor.services.google_services import (
    GA4_DERIVED_FIELDS,
    GSC_DERIVED_FIELDS,
    AssignmentResult,
    apply_google_services,
    reset_derived_fields,
)

User = get_user_model()


def _patch_fetching(ga4_side_effect=None, gsc_side_effect=None):
    """Podmienia pobieranie danych z Google na potrzeby jednego wywołania."""
    return (
        patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ),
        patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data",
            side_effect=ga4_side_effect,
        ),
        patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data",
            side_effect=gsc_side_effect,
        ),
    )


class GoogleServicesBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="uslugi@przyklad.pl",
            email="uslugi@przyklad.pl",
            password="haslo-kontrolne-1",
        )

    def setUp(self):
        self.client.force_login(self.user)
        cache.clear()

    def _audit(self, **kwargs) -> Audit:
        dane = {
            "url": "https://enova.pl/",
            "owner": self.user,
            "ga4_organic_sessions": 5000,
            "ga4_history": {"2026-01": 5000},
            "ga4_insights": {"wniosek": "stare"},
            "ga4_selected_lead_event": "stare_zdarzenie",
            "gsc_total_clicks_current": 900,
            "gsc_query_commentary": "stary komentarz",
        }
        dane.update(kwargs)
        audit = Audit.objects.create(**dane)
        Audit.objects.filter(pk=audit.pk).update(
            ga4_refresh_token_encrypted="jawny-token-testowy"
        )
        return Audit.objects.get(pk=audit.pk)


class ApplyServicesTests(GoogleServicesBase):
    """Warstwa serwisowa - sedno scalenia."""

    def test_first_assignment_saves_and_fetches(self):
        audit = self._audit(ga4_property_id=None, gsc_site_url="")

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data") as sync_ga4, \
             patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            wynik = apply_google_services(audit, "111111", "sc-domain:enova.pl")

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_property_id, "111111")
        self.assertEqual(audit.gsc_site_url, "sc-domain:enova.pl")
        self.assertTrue(wynik.changed)
        self.assertTrue(wynik.fetched)
        sync_ga4.assert_called_once()

    def test_first_assignment_also_clears_derived_data(self):
        # Audyt bez przypisanej usługi może mieć dane z automatycznego dopasowania.
        audit = self._audit(ga4_property_id=None, gsc_site_url="")

        for p in _patch_fetching():
            p.start()
        apply_google_services(audit, "111111", "")
        for p in _patch_fetching():
            p.stop()

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_organic_sessions, 0)
        self.assertIsNone(audit.ga4_selected_lead_event)

    def test_changing_property_clears_ga4_data(self):
        audit = self._audit(ga4_property_id="999999")

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), \
             patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            apply_google_services(audit, "111111", "")

        audit.refresh_from_db()
        self.assertEqual(audit.ga4_organic_sessions, 0)
        self.assertEqual(audit.ga4_history, {})
        self.assertEqual(audit.ga4_insights, {})

    def test_changing_site_clears_gsc_data(self):
        audit = self._audit(gsc_site_url="sc-domain:orlen.pl")

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), \
             patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            apply_google_services(audit, "", "sc-domain:enova.pl")

        audit.refresh_from_db()
        self.assertEqual(audit.gsc_total_clicks_current, 0)
        self.assertEqual(audit.gsc_query_commentary, "")

    def test_unchanged_selection_touches_nothing(self):
        audit = self._audit(ga4_property_id="999999", gsc_site_url="sc-domain:orlen.pl")

        with patch("auditor.services.audit_service.AuditService.sync_ga4_data") as sync_ga4:
            wynik = apply_google_services(audit, "999999", "sc-domain:orlen.pl")

        audit.refresh_from_db()
        self.assertFalse(wynik.changed)
        self.assertEqual(audit.ga4_organic_sessions, 5000)
        sync_ga4.assert_not_called()

    def test_stale_caches_are_dropped(self):
        audit = self._audit(ga4_property_id="999999")
        cache.set(f"ga4_properties:{audit.pk}", ["cokolwiek"], 300)
        cache.set("ga4_events:999999", ["stare_zdarzenie"], 300)

        for p in _patch_fetching():
            p.start()
        apply_google_services(audit, "111111", "")
        for p in _patch_fetching():
            p.stop()

        self.assertIsNone(cache.get(f"ga4_properties:{audit.pk}"))
        self.assertIsNone(cache.get("ga4_events:999999"))

    def test_failed_fetch_is_reported_not_raised(self):
        audit = self._audit(ga4_property_id="999999")

        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch(
            "auditor.services.audit_service.AuditService.sync_ga4_data",
            side_effect=Exception("Google nie odpowiada"),
        ), patch(
            "auditor.services.audit_service.AuditService.sync_gsc_data",
            side_effect=Exception("Google nie odpowiada"),
        ):
            wynik = apply_google_services(audit, "111111", "")

        audit.refresh_from_db()
        self.assertTrue(wynik.changed)
        self.assertFalse(wynik.fetched)
        # Puste liczby są uczciwe; cudze nie.
        self.assertEqual(audit.ga4_organic_sessions, 0)

    def test_no_token_means_no_fetching(self):
        audit = self._audit(ga4_property_id="999999")
        Audit.objects.filter(pk=audit.pk).update(ga4_refresh_token_encrypted=None)
        audit.refresh_from_db()

        wynik = apply_google_services(audit, "111111", "")

        self.assertTrue(wynik.changed)
        self.assertFalse(wynik.fetched)


class ResultTests(TestCase):
    """Drobny typ opisujący, co się wydarzyło."""

    def test_changed_is_true_when_either_part_changed(self):
        self.assertTrue(AssignmentResult(ga4_changed=True).changed)
        self.assertTrue(AssignmentResult(gsc_changed=True).changed)
        self.assertFalse(AssignmentResult().changed)

    def test_reset_handles_callables_and_plain_values(self):
        class Atrapa:
            pass

        obiekt = Atrapa()
        zmienione = reset_derived_fields(obiekt, {"liczba": 0, "slownik": dict, "lista": list})

        self.assertEqual(obiekt.liczba, 0)
        self.assertEqual(obiekt.slownik, {})
        self.assertEqual(obiekt.lista, [])
        self.assertEqual(sorted(zmienione), ["liczba", "lista", "slownik"])

    def test_every_derived_field_exists_on_the_model(self):
        # Literówka w nazwie pola przeszłaby niezauważona - `setattr` ją przyjmie,
        # a `save(update_fields=...)` dopiero wtedy rzuci błędem.
        nazwy = {f.name for f in Audit._meta.get_fields()}

        for pole in list(GA4_DERIVED_FIELDS) + list(GSC_DERIVED_FIELDS):
            with self.subTest(pole=pole):
                self.assertIn(pole, nazwy)


class BothPathsAgreeTests(GoogleServicesBase):
    """Obie drogi kończą się tym samym stanem bazy."""

    def _assign(self, audit: Audit, next_target: str):
        with patch(
            "auditor.services.google_services.build_credentials_from_refresh_token",
            return_value=MagicMock(),
        ), patch("auditor.services.audit_service.AuditService.sync_ga4_data"), \
             patch("auditor.services.audit_service.AuditService.sync_gsc_data"):
            return self.client.post(
                reverse("auditor:assign_google_services", args=[audit.pk]),
                {
                    "ga4_property_id": "111111",
                    "gsc_site_url": "",
                    "next": next_target,
                },
            )

    def _snapshot(self, audit: Audit) -> dict:
        audit.refresh_from_db()
        return {
            "ga4_property_id": audit.ga4_property_id,
            "ga4_organic_sessions": audit.ga4_organic_sessions,
            "ga4_history": audit.ga4_history,
            "ga4_insights": audit.ga4_insights,
            "ga4_selected_lead_event": audit.ga4_selected_lead_event,
            "gsc_total_clicks_current": audit.gsc_total_clicks_current,
        }

    def test_post_oauth_path_and_panel_path_leave_identical_state(self):
        po_oauth = self._audit(url="https://a.przyklad.pl/", ga4_property_id="999999")
        z_panelu = self._audit(url="https://b.przyklad.pl/", ga4_property_id="999999")

        self._assign(po_oauth, "detail")
        self._assign(z_panelu, "analytics")

        self.assertEqual(self._snapshot(po_oauth), self._snapshot(z_panelu))

    def test_only_the_return_target_differs(self):
        po_oauth = self._audit(url="https://a.przyklad.pl/", ga4_property_id="999999")
        z_panelu = self._audit(url="https://b.przyklad.pl/", ga4_property_id="999999")

        self.assertEqual(
            self._assign(po_oauth, "detail")["Location"], f"/audits/{po_oauth.pk}/"
        )
        self.assertEqual(
            self._assign(z_panelu, "analytics")["Location"],
            f"/audits/{z_panelu.pk}/analytics/",
        )

    def test_unknown_return_target_falls_back_to_this_audit(self):
        # `next` przychodzi od klienta - nie może posłużyć do wyprowadzenia
        # użytkownika poza aplikację.
        audit = self._audit(ga4_property_id="999999")

        response = self._assign(audit, "https://zlosliwa-strona.example/")

        self.assertEqual(response["Location"], f"/audits/{audit.pk}/analytics/")


class SelectionScreenTests(GoogleServicesBase):
    """Ekran po autoryzacji sam już nie zapisuje."""

    def test_screen_renders_the_choices(self):
        audit = self._audit()
        cache.set(
            f"ga4_properties:{audit.pk}",
            [{"property_id": "111111", "display_name": "enova.pl"}],
            300,
        )

        response = self.client.get(
            reverse("auditor:select_ga4_property", args=[audit.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "111111")

    def test_screen_rejects_post(self):
        # Zapis ma jedno wejście - to zabezpiecza przed powrotem drugiej ścieżki.
        audit = self._audit()

        response = self.client.post(
            reverse("auditor:select_ga4_property", args=[audit.pk]),
            {"ga4_property_id": "111111"},
        )

        self.assertEqual(response.status_code, 405)

    def test_form_targets_the_unified_endpoint(self):
        audit = self._audit()
        cache.set(
            f"ga4_properties:{audit.pk}",
            [{"property_id": "111111", "display_name": "enova.pl"}],
            300,
        )

        html = self.client.get(
            reverse("auditor:select_ga4_property", args=[audit.pk])
        ).content.decode()

        self.assertIn(f'action="/analytics/{audit.pk}/assign/"', html)
        self.assertIn('name="ga4_property_id"', html)
        self.assertIn('name="next" value="detail"', html)
