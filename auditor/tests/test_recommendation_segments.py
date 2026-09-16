"""Testy podziału rekomendacji AI na segmenty (nagłówki, akapity, bloki kodu).

Regresja: po wymuszeniu w prompcie czterech sekcji `### N. NAZWA` znaczniki Markdown
trafiały dosłownie do `<p>` - użytkownik widział w interfejsie "### 1. DIAGNOZA...".
"""
from __future__ import annotations

from django.test import SimpleTestCase

from auditor.presentation import split_recommendation_segments

PELNA_REKOMENDACJA = """### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA
Obraz nagłówkowy ma atrybut loading="lazy", więc przeglądarka pobiera go na końcu.

### 2. PLAN DZIAŁANIA (KROK PO KROKU)
1. Usuń loading="lazy".
2. Dodaj fetchpriority="high".

### 3. GOTOWA RECEPTA KODOWA / KONFIGURACJA
```html
<img src="/hero.avif" fetchpriority="high" width="1920" height="900" alt="...">
```
Obecnie: stary znacznik -> Proponowane: nowy znacznik."""


class SegmentacjaNaglowkowTests(SimpleTestCase):
    def test_pelna_rekomendacja_dzieli_sie_na_sekcje(self):
        segmenty = split_recommendation_segments(PELNA_REKOMENDACJA)
        typy = [s["type"] for s in segmenty]

        self.assertEqual(
            typy,
            ["heading", "text", "heading", "text", "heading", "code", "text"],
        )

    def test_naglowki_traca_znaczniki_markdown(self):
        naglowki = [s["content"] for s in split_recommendation_segments(PELNA_REKOMENDACJA)
                    if s["type"] == "heading"]

        self.assertEqual(
            naglowki,
            ["1. DIAGNOZA I PRZYCZYNA TECHNICZNA",
             "2. PLAN DZIAŁANIA (KROK PO KROKU)",
             "3. GOTOWA RECEPTA KODOWA / KONFIGURACJA"],
        )

    def test_zaden_segment_tekstowy_nie_zawiera_krzyzykow(self):
        """To jest dokładnie ten defekt, który zmiana naprawia."""
        for segment in split_recommendation_segments(PELNA_REKOMENDACJA):
            if segment["type"] == "text":
                with self.subTest(fragment=segment["content"][:40]):
                    self.assertNotIn("###", segment["content"])

    def test_kod_pozostaje_nietkniety(self):
        kod = [s["content"] for s in split_recommendation_segments(PELNA_REKOMENDACJA)
               if s["type"] == "code"]

        self.assertEqual(len(kod), 1)
        self.assertIn('fetchpriority="high"', kod[0])

    def test_krzyzyk_w_bloku_kodu_nie_jest_naglowkiem(self):
        """Komentarz Nginx czy Python zaczyna się od # - nie wolno go wyciąć."""
        segmenty = split_recommendation_segments(
            "Konfiguracja serwera:\n```nginx\n# Przekierowanie kanoniczne\nreturn 301 https://a.pl;\n```"
        )

        kod = [s for s in segmenty if s["type"] == "code"][0]["content"]
        self.assertIn("# Przekierowanie kanoniczne", kod)
        self.assertEqual([s["type"] for s in segmenty], ["text", "code"])

    def test_pojedynczy_krzyzyk_nie_jest_traktowany_jak_naglowek(self):
        """Wiersz zaczynający się od jednego # to zwykle komentarz, nie sekcja."""
        segmenty = split_recommendation_segments("# to nie jest nagłówek sekcji")

        self.assertEqual([s["type"] for s in segmenty], ["text"])

    def test_rekomendacja_bez_naglowkow_dziala_jak_dotad(self):
        """Fallback bazowy i starsze audyty nie mają sekcji - nie mogą się zepsuć."""
        segmenty = split_recommendation_segments(
            "Dodaj atrybut alt do grafiki.\n```html\n<img src=\"a.jpg\" alt=\"Opis\">\n```"
        )

        self.assertEqual([s["type"] for s in segmenty], ["text", "code"])

    def test_pusta_rekomendacja_zwraca_pusta_liste(self):
        self.assertEqual(split_recommendation_segments(None), [])
        self.assertEqual(split_recommendation_segments(""), [])

    def test_naglowek_bez_tresci_pod_spodem_nie_gubi_sie(self):
        segmenty = split_recommendation_segments("### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA")

        self.assertEqual(
            segmenty, [{"type": "heading", "content": "1. DIAGNOZA I PRZYCZYNA TECHNICZNA"}]
        )
