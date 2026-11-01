"""Formularz konfiguracji na panelu analityki - układ i ergonomia.

Zgłoszone objawy: dwa selekty ściśnięte obok siebie w kafelku o 1/3 szerokości
(nazwy usług ucięte do "motiv..."), adres URL łamany w środku słowa
("https://www.moti / vationdirect.pl/") i brak wyjścia z edycji innego niż zapis.

Przyczyną ściśnięcia była KOLEJNOŚĆ reguł w arkuszu: modyfikator
`.analytics-config-form-stacked` stał przed `.analytics-config-form`, a przy równej
wadze (jedna klasa) wygrywa reguła późniejsza - układ dwukolumnowy nadpisywał więc
jednokolumnowy. Stąd test pilnujący kolejności, a nie samej obecności reguły.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR) / "auditor/templates/auditor/base.html"
PANEL = Path(settings.BASE_DIR) / "auditor/templates/auditor/analytics_dashboard.html"
SELEKTOR_GA4 = Path(settings.BASE_DIR) / "auditor/templates/auditor/_ga4_property_select.html"


class StackedLayoutTests(SimpleTestCase):
    """Pola jedno pod drugim, na pełną szerokość."""

    @property
    def css(self) -> str:
        return BASE.read_text(encoding="utf-8")

    def test_the_stacked_variant_overrides_the_two_column_layout(self):
        # Sedno usterki: obie reguły mają tę samą wagę, więc liczy się kolejność.
        bazowa = self.css.index(".analytics-config-form {")
        pionowa = self.css.index(".analytics-config-form-stacked {")

        self.assertGreater(
            pionowa, bazowa,
            "Modyfikator `-stacked` musi stać PO regule bazowej, inaczej przegrywa: "
            "jego `display: flex` nie nadpisze `display: grid`, a pola wrócą do "
            "dwóch kolumn.",
        )

    def test_selects_fill_the_whole_field(self):
        self.assertIn(".analytics-config-field select { min-width: 0; width: 100%; }", self.css)

    def test_the_form_is_a_single_vertical_column(self):
        fragment = self.css[self.css.index(".analytics-config-form-stacked {"):]
        fragment = fragment[: fragment.index("}")]

        self.assertIn("display: flex", fragment)
        self.assertIn("flex-direction: column", fragment)
        self.assertIn("width: 100%", fragment)
        self.assertIn("gap: 12px", fragment)

    def test_the_card_keeps_its_standard_width(self):
        # Rozpychanie kafelka na całą siatkę psuło układ sąsiednich kart GA4 i GSC.
        self.assertNotIn(":has(#analytics-config", self.css)

    def test_the_narrow_card_does_not_stretch_the_selects(self):
        # Globalny `select` ma `min-width: 260px` - w kafelku o 1/3 szerokości
        # rozpychałby kolumnę poza jej obrys.
        self.assertIn(
            ".analytics-config-form-stacked select,\n"
            "        .analytics-config-form-stacked .analytics-config-field "
            "{ width: 100%; min-width: 0; }",
            self.css,
        )

    def test_both_buttons_stay_in_one_row(self):
        fragment = self.css[
            self.css.index(".analytics-config-form-stacked .analytics-config-actions {"):
        ]
        fragment = fragment[: fragment.index("}")]

        self.assertIn("flex-wrap: nowrap", fragment)


class CurrentValuePresentationTests(SimpleTestCase):
    """Adres witryny jako znacznik, bez łamania w losowym miejscu."""

    def test_the_value_is_rendered_as_a_badge_with_the_full_text_in_a_tooltip(self):
        panel = PANEL.read_text(encoding="utf-8")

        self.assertIn(
            '<span class="analytics-config-value" title="{{ audit.gsc_site_url }}">',
            panel,
        )

    def test_the_badge_is_monospace_and_truncated_instead_of_broken(self):
        css = BASE.read_text(encoding="utf-8")
        fragment = css[css.index(".analytics-config-value {"):]
        fragment = fragment[: fragment.index("}")]

        self.assertIn("monospace", fragment)
        self.assertIn("text-overflow: ellipsis", fragment)
        self.assertIn("white-space: nowrap", fragment)

    def test_the_sentence_is_one_flex_item(self):
        # Bez tego opakowania tekst i adres są osobnymi elementami flex, a zdanie
        # łamie się na kolumny wokół adresu.
        panel = PANEL.read_text(encoding="utf-8")
        fragment = panel[panel.index('<p class="analytics-auto-gsc">'):]
        fragment = fragment[: fragment.index("</p>")]

        self.assertIn('class="analytics-auto-gsc-text"', fragment)

    def test_long_option_labels_keep_their_full_text_in_a_tooltip(self):
        # `<select>` ucina etykiety bez litości, a konta agencyjne mają długie nazwy.
        selektor = SELEKTOR_GA4.read_text(encoding="utf-8")

        self.assertEqual(selektor.count("title=\"{% if option.account %}"), 2)


class CancelButtonTests(SimpleTestCase):
    """Wyjście z edycji bez zapisu."""

    def test_the_cancel_button_sits_next_to_save(self):
        panel = PANEL.read_text(encoding="utf-8")
        fragment = panel[panel.index('<div class="analytics-config-actions">'):]
        fragment = fragment[: fragment.index("</div>")]

        self.assertIn('type="submit"', fragment)
        self.assertIn('id="analytics-config-cancel"', fragment)

    def test_the_cancel_button_does_not_submit_the_form(self):
        panel = PANEL.read_text(encoding="utf-8")
        fragment = panel[panel.index('id="analytics-config-cancel"') - 200:]
        fragment = fragment[: fragment.index("</button>")]

        self.assertIn('type="button"', fragment)

    def test_cancel_collapses_the_section_and_restores_the_values(self):
        panel = PANEL.read_text(encoding="utf-8")

        self.assertIn("formularz.reset()", panel)
        self.assertIn("sekcja.open = false", panel)

    def test_cancel_clears_the_anchor_so_a_refresh_keeps_it_closed(self):
        # Kotwica `#analytics-config` otwiera sekcję przy wejściu - po anulowaniu
        # odświeżenie strony otwierałoby ją z powrotem.
        self.assertIn("history.replaceState", PANEL.read_text(encoding="utf-8"))
