"""Przyciski odświeżenia w ostrzeżeniu o nieaktualnych danych.

Banner miał JEDEN przycisk prowadzący zawsze do skanera technicznego, więc
ostrzeżenie o nieaktualnej analityce otwierało narzędzie, które analityki nie
dotyka. Błąd jest mylący podwójnie: użytkownik trafia nie tam, a potem uruchamia
skan, który nie odświeży danych, o które mu chodziło.

Żadne wywołanie Google ani OpenAI nie jest tu prawdziwe.
"""
from __future__ import annotations

import re

from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from auditor.agents.state import SEOInvestigatorState
from auditor.models import Audit, InvestigationRun
from auditor.presentation import attach_stale_actions, build_investigation_context

User = get_user_model()

DAWNO = date.today() - timedelta(days=40)


def _body_only(html: str) -> str:
    """HTML bez arkusza stylów - nazwa klasy w CSS nie znaczy, że element istnieje."""
    return re.sub(r"\s+", " ", re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL))


def _state(audit_id=None, **daty) -> SEOInvestigatorState:
    state = SEOInvestigatorState(domain="earlystage.pl")
    state.audit_id = audit_id
    state.data_timestamps.update(daty)
    return state


class StaleActionTests(SimpleTestCase):
    """`attach_stale_actions` - adres narzędzia dla każdego źródła."""

    def _akcje(self, state, audit=None):
        return {
            wpis["source"]: wpis
            for wpis in build_investigation_context(state, audit=audit)["stale_data"]
        }

    def test_each_source_points_at_its_own_tool(self):
        akcje = self._akcje(_state(technical=DAWNO, analytics=DAWNO, geo=DAWNO))

        self.assertEqual(akcje["technical"]["action_url"], reverse("auditor:index"))
        self.assertEqual(akcje["analytics"]["action_url"], reverse("auditor:analytics"))
        self.assertEqual(akcje["geo"]["action_url"], reverse("auditor:geo_dashboard"))

    def test_analytics_never_points_at_the_technical_scanner(self):
        # Sedno zgłoszenia.
        akcje = self._akcje(_state(analytics=DAWNO))

        self.assertNotEqual(
            akcje["analytics"]["action_url"], reverse("auditor:index")
        )

    def test_analytics_opens_the_panel_of_this_domain(self):
        # Analityka ma własny ekran per domena - prowadzimy wprost do niego.
        audyt = Audit(pk=42, url="https://earlystage.pl/")

        akcje = self._akcje(_state(analytics=DAWNO), audit=audyt)

        self.assertEqual(
            akcje["analytics"]["action_url"],
            reverse("auditor:audit_analytics", args=[42]),
        )

    def test_audit_found_by_the_agent_is_enough(self):
        # Wejście z hubu nie zna audytu, ale agent techniczny mógł go znaleźć.
        akcje = self._akcje(_state(audit_id=77, analytics=DAWNO))

        self.assertEqual(
            akcje["analytics"]["action_url"],
            reverse("auditor:audit_analytics", args=[77]),
        )

    def test_without_an_audit_the_general_entry_is_used(self):
        akcje = self._akcje(_state(analytics=DAWNO))

        self.assertEqual(akcje["analytics"]["action_url"], reverse("auditor:analytics"))

    def test_every_action_has_a_label(self):
        akcje = self._akcje(_state(technical=DAWNO, analytics=DAWNO, geo=DAWNO))

        for zrodlo, wpis in akcje.items():
            with self.subTest(zrodlo=zrodlo):
                self.assertTrue(wpis["action_label"])

    def test_labels_name_the_target_tool(self):
        akcje = self._akcje(_state(technical=DAWNO, analytics=DAWNO, geo=DAWNO))

        self.assertIn("audyt techniczny", akcje["technical"]["action_label"].lower())
        self.assertIn("analitykę", akcje["analytics"]["action_label"].lower())
        self.assertIn("widoczność", akcje["geo"]["action_label"].lower())

    def test_fresh_sources_produce_no_actions(self):
        swieze = _state(technical=date.today(), analytics=date.today())

        self.assertEqual(build_investigation_context(swieze)["stale_data"], [])

    def test_unknown_source_falls_back_to_the_hub(self):
        # Czwarte źródło dodane w przyszłości nie może dać przycisku donikąd.
        wpisy = attach_stale_actions([{"source": "nieznane", "message": "x"}])

        self.assertEqual(wpisy[0]["action_url"], reverse("auditor:hub"))


class StaleBannerMarkupTests(TestCase):
    """Wyrenderowany banner na stronie wyniku badania."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="linki@przyklad.pl",
            email="linki@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://earlystage.pl/", owner=cls.user, status=Audit.Status.COMPLETED
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _open(self, **daty):
        """Strona wyniku zapisanego badania z podanymi datami źródeł."""
        state = SEOInvestigatorState(domain="earlystage.pl")
        state.technical_issues.append("Błąd: brak meta description.")
        state.mark_source("technical", True)
        state.mark_source("analytics", True)
        state.metrics["technical_score"] = 66
        state.data_timestamps.update(daty)
        state.final_synthesis_report = "## Podsumowanie\nTreść."

        run = InvestigationRun.objects.create(
            domain="earlystage.pl", owner=self.user, audit=self.audit,
            status=InvestigationRun.Status.COMPLETED,
            state=state.as_dict(), report=state.final_synthesis_report,
        )
        return self.client.get(
            reverse("auditor:investigation_detail", args=[run.pk])
        )

    def test_stale_analytics_links_to_analytics(self):
        response = self._open(analytics=DAWNO)

        self.assertContains(
            response, reverse("auditor:audit_analytics", args=[self.audit.pk])
        )
        self.assertContains(response, "Otwórz Analitykę i Ruch")

    def test_stale_analytics_does_not_link_to_the_scanner(self):
        # Adres skanera ("/audits/") jest PREFIKSEM adresu analityki
        # ("/audits/1/analytics/"), więc porównanie po podciągu ich nie rozróżnia.
        # Sprawdzamy pełny atrybut href.
        html = _body_only(self._open(analytics=DAWNO).content.decode())

        self.assertNotIn(
            f'class="stale-banner-link" href="{reverse("auditor:index")}"', html
        )
        self.assertIn(
            'class="stale-banner-link" '
            f'href="{reverse("auditor:audit_analytics", args=[self.audit.pk])}"',
            html,
        )

    def test_stale_technical_links_to_the_scanner(self):
        response = self._open(technical=DAWNO)

        self.assertContains(response, "Uruchom nowy audyt techniczny")
        html = _body_only(response.content.decode())
        self.assertIn(f'href="{reverse("auditor:index")}"', html)

    def test_two_stale_sources_get_two_different_links(self):
        # Oba ostrzeżenia w jednym bannerze muszą prowadzić gdzie indziej.
        html = _body_only(self._open(technical=DAWNO, analytics=DAWNO).content.decode())

        self.assertIn(f'href="{reverse("auditor:index")}"', html)
        self.assertIn(
            f'href="{reverse("auditor:audit_analytics", args=[self.audit.pk])}"', html
        )
        self.assertEqual(html.count('class="stale-banner-link"'), 2)

    def test_single_shared_button_is_gone(self):
        # Wcześniej jeden przycisk na cały banner prowadził zawsze do skanera.
        html = _body_only(self._open(technical=DAWNO, analytics=DAWNO).content.decode())

        self.assertNotIn("Przejdź do skanera", html)
        self.assertNotIn("stale-banner-action", html)

    def test_fresh_data_shows_no_banner_and_no_links(self):
        html = _body_only(self._open(technical=date.today()).content.decode())

        self.assertNotIn('class="panel stale-banner"', html)
        self.assertNotIn("stale-banner-link", html)


class SynthesisLinkAuditTests(TestCase):
    """Pozostałe odnośniki w widoku syntezy - bez pomyłek kopiuj-wklej."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="audyt-linkow@przyklad.pl",
            email="audyt-linkow@przyklad.pl",
            password="haslo-kontrolne-1",
        )
        cls.audit = Audit.objects.create(
            url="https://earlystage.pl/", owner=cls.user, status=Audit.Status.COMPLETED
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _run(self, **pola):
        state = SEOInvestigatorState(domain="earlystage.pl")
        state.mark_source("technical", True)
        state.final_synthesis_report = "## Podsumowanie\nTreść."
        pola.setdefault("state", state.as_dict())
        # Przez `setdefault`, nie jako argument wprost: inaczej wywołanie podające
        # własny status kolidowałoby z tym poniżej.
        pola.setdefault("status", InvestigationRun.Status.COMPLETED)
        pola.setdefault("report", "## Podsumowanie\nTreść.")
        return InvestigationRun.objects.create(
            domain="earlystage.pl", owner=self.user, **pola
        )

    def test_back_button_returns_to_the_technical_report(self):
        run = self._run(audit=self.audit)

        response = self.client.get(
            reverse("auditor:investigation_detail", args=[run.pk])
        )

        self.assertContains(response, reverse("auditor:detail", args=[self.audit.pk]))

    def test_back_button_returns_to_the_hub_without_an_audit(self):
        run = self._run()

        response = self.client.get(
            reverse("auditor:investigation_detail", args=[run.pk])
        )

        self.assertContains(response, "Powrót do ekranu głównego")
        self.assertContains(response, f'href="{reverse("auditor:hub")}"')

    def test_hub_tiles_point_at_their_own_tools(self):
        # Kafelki biorą adres z `navigation.TOOLS` - sprawdzamy, że mapowanie
        # narzędzie→trasa nie rozjechało się po żadnej zmianie.
        html = _body_only(self.client.get(reverse("auditor:hub")).content.decode())

        for trasa in ("auditor:analytics", "auditor:index", "auditor:geo_dashboard"):
            with self.subTest(trasa=trasa):
                self.assertIn(f'href="{reverse(trasa)}"', html)

    def test_waiting_page_polls_its_own_run(self):
        run = self._run(status=InvestigationRun.Status.PROCESSING, state={}, report="")

        response = self.client.get(
            reverse("auditor:investigation_detail", args=[run.pk])
        )

        self.assertContains(
            response, reverse("auditor:investigation_status", args=[run.pk])
        )
        self.assertContains(
            response, reverse("auditor:investigation_detail", args=[run.pk])
        )
