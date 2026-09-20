"""Testy podziału rekomendacji AI na segmenty (nagłówki, akapity, bloki kodu).

Regresja: po wymuszeniu w prompcie czterech sekcji `### N. NAZWA` znaczniki Markdown
trafiały dosłownie do `<p>` - użytkownik widział w interfejsie "### 1. DIAGNOZA...".
"""
from __future__ import annotations

from django.test import SimpleTestCase

from auditor.presentation import split_recommendation_segments

FULL_RECOMMENDATION = """### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA
Obraz nagłówkowy ma atrybut loading="lazy", więc przeglądarka pobiera go na końcu.

### 2. PLAN DZIAŁANIA (KROK PO KROKU)
1. Usuń loading="lazy".
2. Dodaj fetchpriority="high".

### 3. GOTOWA RECEPTA KODOWA / KONFIGURACJA
```html
<img src="/hero.avif" fetchpriority="high" width="1920" height="900" alt="...">
```
Obecnie: stary znacznik -> Proponowane: nowy znacznik."""


class HeadingSegmentationTests(SimpleTestCase):
    def test_full_recommendation_splits_into_sections(self):
        segments = split_recommendation_segments(FULL_RECOMMENDATION)
        types = [s["type"] for s in segments]

        self.assertEqual(
            types,
            ["heading", "text", "heading", "text", "heading", "code", "text"],
        )

    def test_headings_lose_their_markdown_markers(self):
        headings = [s["content"] for s in split_recommendation_segments(FULL_RECOMMENDATION)
                    if s["type"] == "heading"]

        self.assertEqual(
            headings,
            ["1. DIAGNOZA I PRZYCZYNA TECHNICZNA",
             "2. PLAN DZIAŁANIA (KROK PO KROKU)",
             "3. GOTOWA RECEPTA KODOWA / KONFIGURACJA"],
        )

    def test_no_text_segment_contains_hash_marks(self):
        """To jest dokładnie ten defekt, który zmiana naprawia."""
        for segment in split_recommendation_segments(FULL_RECOMMENDATION):
            if segment["type"] == "text":
                with self.subTest(fragment=segment["content"][:40]):
                    self.assertNotIn("###", segment["content"])

    def test_code_block_stays_untouched(self):
        code = [s["content"] for s in split_recommendation_segments(FULL_RECOMMENDATION)
               if s["type"] == "code"]

        self.assertEqual(len(code), 1)
        self.assertIn('fetchpriority="high"', code[0])

    def test_hash_inside_a_code_block_is_not_a_heading(self):
        """Komentarz Nginx czy Python zaczyna się od # - nie wolno go wyciąć."""
        segments = split_recommendation_segments(
            "Konfiguracja serwera:\n```nginx\n# Przekierowanie kanoniczne\nreturn 301 https://a.pl;\n```"
        )

        code = [s for s in segments if s["type"] == "code"][0]["content"]
        self.assertIn("# Przekierowanie kanoniczne", code)
        self.assertEqual([s["type"] for s in segments], ["text", "code"])

    def test_single_hash_is_not_treated_as_a_heading(self):
        """Wiersz zaczynający się od jednego # to zwykle komentarz, nie sekcja."""
        segments = split_recommendation_segments("# to nie jest nagłówek sekcji")

        self.assertEqual([s["type"] for s in segments], ["text"])

    def test_recommendation_without_headings_still_works(self):
        """Fallback bazowy i starsze audyty nie mają sekcji - nie mogą się zepsuć."""
        segments = split_recommendation_segments(
            "Dodaj atrybut alt do grafiki.\n```html\n<img src=\"a.jpg\" alt=\"Opis\">\n```"
        )

        self.assertEqual([s["type"] for s in segments], ["text", "code"])

    def test_empty_recommendation_returns_an_empty_list(self):
        self.assertEqual(split_recommendation_segments(None), [])
        self.assertEqual(split_recommendation_segments(""), [])

    def test_heading_without_body_is_not_lost(self):
        segments = split_recommendation_segments("### 1. DIAGNOZA I PRZYCZYNA TECHNICZNA")

        self.assertEqual(
            segments, [{"type": "heading", "content": "1. DIAGNOZA I PRZYCZYNA TECHNICZNA"}]
        )
