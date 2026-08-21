from __future__ import annotations

import json
import re

import httpx
from bs4 import BeautifulSoup

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Frazy typowe dla elementów nawigacyjnych/szablonowych, a nie treści merytorycznej.
HX_NOISE_KEYWORDS = (
    "zobacz też",
    "czytaj też",
    "newsletter",
    "popularne",
    "polecane",
    "powiązane",
    "najnowsze wpisy",
    "udostępnij",
    "skomentuj",
    "subskrybuj",
)


class ScraperError(Exception):
    """Podnoszony gdy nie udało się pobrać lub sparsować strony."""


class SEOScraper:
    """Pobiera stronę HTTP i wyciąga z niej dane istotne dla audytu SEO/GEO."""

    def __init__(self, timeout: float = 15.0, user_agent: str | None = None):
        self.timeout = timeout
        self.headers = {
            "User-Agent": user_agent or "SEOAuditorBot/1.0 (+https://example.com)"
        }

    def scrape(self, url: str) -> dict:
        html = self.fetch(url)
        return self.parse(html, url)

    def fetch(self, url: str) -> str:
        try:
            response = httpx.get(
                url, headers=self.headers, timeout=self.timeout, follow_redirects=True
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc
        return response.text

    def parse(self, html: str, url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else None

        meta_description = self._get_meta_content(soup, "description")

        headings = {
            f"h{level}": [h.get_text(strip=True) for h in soup.find_all(f"h{level}")]
            for level in range(1, 7)
        }

        canonical_tag = soup.find("link", rel="canonical")
        canonical = canonical_tag.get("href") if canonical_tag else None

        open_graph = {
            tag["property"][3:]: tag.get("content", "")
            for tag in soup.find_all("meta", property=True)
            if tag["property"].startswith("og:") and tag.get("content")
        }

        images = self._analyze_images(soup.find_all("img"))
        schema = self._extract_schema(soup)
        heading_noise = self._analyze_heading_noise(soup)
        eeat = self._analyze_eeat_signals(soup)

        return {
            "url": url,
            "title": title,
            "title_length": len(title) if title else 0,
            "meta_description": meta_description,
            "meta_description_length": len(meta_description) if meta_description else 0,
            "headings": headings,
            "h1_count": len(headings["h1"]),
            "canonical": canonical,
            "open_graph": open_graph,
            "images_total": images["total"],
            "images_with_alt": images["with_alt"],
            "images_without_alt": images["without_alt"],
            "images_with_title": images["with_title"],
            "images_without_title": images["without_title"],
            "images_non_ascii_src_count": images["non_ascii_src_count"],
            "images_non_ascii_src_examples": images["non_ascii_src_examples"],
            "schema": schema,
            "heading_noise": heading_noise,
            "eeat": eeat,
        }

    def _get_meta_content(self, soup: BeautifulSoup, name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": name})
        content = tag.get("content", "").strip() if tag else ""
        return content or None

    # ------------------------------------------------------------------
    # Analiza obrazków: ALT, title, ASCII w src
    # ------------------------------------------------------------------
    def _analyze_images(self, images: list) -> dict:
        with_alt = [img for img in images if img.get("alt", "").strip()]
        without_alt = [img for img in images if not img.get("alt", "").strip()]
        with_title = [img for img in images if img.get("title", "").strip()]
        without_title = [img for img in images if not img.get("title", "").strip()]
        non_ascii_src = [
            img["src"] for img in images if img.get("src") and not img["src"].isascii()
        ]

        return {
            "total": len(images),
            "with_alt": len(with_alt),
            "without_alt": len(without_alt),
            "with_title": len(with_title),
            "without_title": len(without_title),
            "non_ascii_src_count": len(non_ascii_src),
            "non_ascii_src_examples": non_ascii_src[:5],
        }

    # ------------------------------------------------------------------
    # Dane strukturalne Schema.org (JSON-LD)
    # ------------------------------------------------------------------
    def _extract_schema(self, soup: BeautifulSoup) -> dict:
        scripts = soup.find_all("script", type="application/ld+json")
        types_found: set[str] = set()
        parse_errors = 0

        for script in scripts:
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                parse_errors += 1
                continue
            types_found |= self._collect_schema_types(payload)

        return {
            "blocks_found": len(scripts),
            "types_found": sorted(types_found),
            "parse_errors": parse_errors,
        }

    def _collect_schema_types(self, node) -> set[str]:
        types: set[str] = set()
        if isinstance(node, list):
            for item in node:
                types |= self._collect_schema_types(item)
        elif isinstance(node, dict):
            type_value = node.get("@type")
            if isinstance(type_value, str):
                types.add(type_value)
            elif isinstance(type_value, list):
                types.update(t for t in type_value if isinstance(t, str))
            if isinstance(node.get("@graph"), list):
                types |= self._collect_schema_types(node["@graph"])
        return types

    # ------------------------------------------------------------------
    # Szum nagłówkowy (Hx Noise) i kolejność nagłówków względem H1
    # ------------------------------------------------------------------
    def _analyze_heading_noise(self, soup: BeautifulSoup) -> dict:
        ordered_headings = soup.find_all(HEADING_TAGS)
        first_h1_index = next(
            (i for i, tag in enumerate(ordered_headings) if tag.name == "h1"), None
        )

        headings_before_h1 = []
        if first_h1_index is not None:
            headings_before_h1 = [
                {"tag": tag.name, "text": tag.get_text(strip=True)}
                for tag in ordered_headings[:first_h1_index]
                if tag.name in ("h2", "h3")
            ]

        noisy_headings = []
        for tag in soup.find_all(("h3", "h4")):
            text = tag.get_text(strip=True)
            if any(keyword in text.lower() for keyword in HX_NOISE_KEYWORDS):
                noisy_headings.append({"tag": tag.name, "text": text})

        return {
            "headings_before_h1": headings_before_h1,
            "noisy_headings": noisy_headings,
        }

    # ------------------------------------------------------------------
    # Sygnały E-E-A-T: autorstwo i aktualność treści
    # ------------------------------------------------------------------
    def _analyze_eeat_signals(self, soup: BeautifulSoup) -> dict:
        rel_author = soup.find_all(rel="author")
        itemprop_author = soup.find_all(attrs={"itemprop": "author"})
        class_author = soup.find_all(class_=re.compile(r"author", re.I))
        has_author_signal = bool(rel_author or itemprop_author or class_author)

        modified_tag = soup.find("meta", property="article:modified_time")
        published_tag = soup.find("meta", property="article:published_time")

        return {
            "has_author_signal": has_author_signal,
            "modified_time": modified_tag.get("content") if modified_tag else None,
            "published_time": published_tag.get("content") if published_tag else None,
        }
