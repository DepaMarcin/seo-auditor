from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)

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

# ------------------------------------------------------------------
# Klasyfikacja typu podstrony (używana do walidacji oczekiwanych typów Schema.org)
# ------------------------------------------------------------------
PAGE_TYPE_HOMEPAGE = "homepage"
PAGE_TYPE_PRODUCT = "product"
PAGE_TYPE_ARTICLE = "article"
PAGE_TYPE_CATEGORY = "category"
PAGE_TYPE_GENERIC = "generic"

PRODUCT_PATH_SEGMENTS = ("/produkt/", "/p/", "/item/")
ARTICLE_PATH_SEGMENTS = ("/blog/", "/artykul/")
CATEGORY_PATH_SEGMENTS = ("/kategoria/", "/category/")

_CART_OR_PRICE_CLASS_RE = re.compile(
    r"add-to-cart|add_to_cart|dodaj-do-koszyka|do-koszyka|\bkoszyk\b|\bprice\b|\bcena\b", re.I
)
_PRODUCT_LIST_CLASS_RE = re.compile(
    r"product-item|product-card|product-list|produkt-item|products-list|listing-item", re.I
)

# Frazy typowe dla nagłówków sekcji FAQ.
FAQ_HEADING_KEYWORDS = (
    "faq",
    "często zadawane pytania",
    "czesto zadawane pytania",
    "pytania i odpowiedzi",
)
_FAQ_CONTAINER_CLASS_RE = re.compile(r"\bfaq\b|accordion", re.I)
_FAQ_CONTAINER_ID_RE = re.compile(r"\bfaq\b", re.I)
_SCHEMA_QUESTION_TYPE_RE = re.compile(r"schema\.org/Question", re.I)


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
        normalized_url = self._normalize_url(url)
        html = self.fetch(normalized_url)
        return self.parse(html, normalized_url)

    def fetch(self, url: str) -> str:
        try:
            response = httpx.get(
                url, headers=self.headers, timeout=self.timeout, follow_redirects=True
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc
        return response.text

    def _normalize_url(self, url: str) -> str:
        """httpx (i większość serwerów) odrzuca adresy bez schematu lub z otaczającymi
        białymi znakami - np. "example.com" albo " https://example.com " - dlatego
        normalizujemy je przed wysłaniem żądania, zamiast przerywać audyt ScraperError."""
        normalized = (url or "").strip()
        if normalized and not _SCHEME_RE.match(normalized):
            normalized = f"https://{normalized}"
        return normalized

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
        page_type = self._detect_page_type(url, soup)
        faq_detected = self._detect_faq_section(soup)
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
            "images_without_alt_examples": images["without_alt_examples"],
            "images_with_title": images["with_title"],
            "images_without_title": images["without_title"],
            "images_non_ascii_src_count": images["non_ascii_src_count"],
            "images_non_ascii_src_examples": images["non_ascii_src_examples"],
            "schema": schema,
            "page_type": page_type,
            "faq_detected": faq_detected,
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
        without_alt_src = [img["src"] for img in without_alt if img.get("src")]

        return {
            "total": len(images),
            "with_alt": len(with_alt),
            "without_alt": len(without_alt),
            "without_alt_examples": without_alt_src[:10],
            "with_title": len(with_title),
            "without_title": len(without_title),
            "non_ascii_src_count": len(non_ascii_src),
            "non_ascii_src_examples": non_ascii_src[:5],
        }

    # ------------------------------------------------------------------
    # Dane strukturalne Schema.org (JSON-LD + Microdata)
    # ------------------------------------------------------------------
    def _extract_schema(self, soup: BeautifulSoup) -> dict:
        scripts = soup.find_all("script", type="application/ld+json")
        json_ld_types: set[str] = set()
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
            json_ld_types |= self._collect_schema_types(payload)

        microdata_types = self._extract_microdata_types(soup)
        types_found = json_ld_types | microdata_types

        return {
            "blocks_found": len(scripts),
            "types_found": sorted(types_found),
            "json_ld_types": sorted(json_ld_types),
            "microdata_types": sorted(microdata_types),
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

    def _extract_microdata_types(self, soup: BeautifulSoup) -> set[str]:
        """Wyciąga typy Schema.org zapisane jako Microdata (itemscope + itemtype),
        np. <div itemscope itemtype="https://schema.org/Product">."""
        types: set[str] = set()
        for tag in soup.find_all(attrs={"itemscope": True}):
            itemtype = tag.get("itemtype", "")
            if not itemtype:
                continue
            for token in itemtype.split():
                type_name = token.rstrip("/").rsplit("/", 1)[-1]
                if type_name:
                    types.add(type_name)
        return types

    # ------------------------------------------------------------------
    # Klasyfikacja typu podstrony (Strona główna / Produkt / Artykuł / Kategoria / Ogólna)
    # ------------------------------------------------------------------
    def _detect_page_type(self, url: str, soup: BeautifulSoup) -> str:
        path = (urlparse(url).path or "/").lower()

        if path in ("", "/"):
            return PAGE_TYPE_HOMEPAGE

        if any(segment in path for segment in PRODUCT_PATH_SEGMENTS) or self._has_product_signals(soup):
            return PAGE_TYPE_PRODUCT

        if any(segment in path for segment in ARTICLE_PATH_SEGMENTS) or self._has_article_signals(soup):
            return PAGE_TYPE_ARTICLE

        if any(segment in path for segment in CATEGORY_PATH_SEGMENTS) or self._has_category_signals(soup):
            return PAGE_TYPE_CATEGORY

        return PAGE_TYPE_GENERIC

    def _has_product_signals(self, soup: BeautifulSoup) -> bool:
        # Przyciski "dodaj do koszyka" lub elementy z klasą ceny.
        return bool(soup.find(attrs={"class": _CART_OR_PRICE_CLASS_RE}))

    def _has_article_signals(self, soup: BeautifulSoup) -> bool:
        if soup.find("article"):
            return True
        author_meta = soup.find("meta", attrs={"name": "author"}) or soup.find(
            "meta", property="article:author"
        )
        date_meta = soup.find("meta", property="article:published_time") or soup.find(
            "meta", property="article:modified_time"
        )
        return bool(author_meta or date_meta)

    def _has_category_signals(self, soup: BeautifulSoup) -> bool:
        # Kilka powtarzalnych elementów listy produktów sugeruje stronę kategorii/sklepu.
        return len(soup.find_all(attrs={"class": _PRODUCT_LIST_CLASS_RE})) >= 2

    # ------------------------------------------------------------------
    # Dynamiczna detekcja sekcji FAQ
    # ------------------------------------------------------------------
    def _detect_faq_section(self, soup: BeautifulSoup) -> bool:
        for tag in soup.find_all(HEADING_TAGS):
            text = tag.get_text(strip=True).lower()
            if any(keyword in text for keyword in FAQ_HEADING_KEYWORDS):
                return True

        # Wiele elementów <details> to typowy wzorzec akordeonu z pytaniami/odpowiedziami.
        if len(soup.find_all("details")) >= 2:
            return True

        if soup.find(attrs={"class": _FAQ_CONTAINER_CLASS_RE}):
            return True
        if soup.find(attrs={"id": _FAQ_CONTAINER_ID_RE}):
            return True

        # Microdata Question/Answer (wzorzec Q&A niezależny od bloku JSON-LD FAQPage).
        if soup.find(attrs={"itemtype": _SCHEMA_QUESTION_TYPE_RE}):
            return True

        return False

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
