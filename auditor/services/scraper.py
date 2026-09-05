from __future__ import annotations

import copy
import json
import re
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup

from .url_guard import MAX_REDIRECT_HOPS, UnsafeUrlError, validate_public_url

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Próg (liczba słów widocznego tekstu) i minimalna liczba <script>, poniżej/powyżej
# których strona jest podejrzewana o renderowanie wyłącznie po stronie klienta (CSR) -
# treść "pusta" bez wykonania JS jest niewidoczna dla części robotów/modeli LLM.
JS_CSR_WORD_COUNT_THRESHOLD = 80
JS_CSR_MIN_SCRIPT_COUNT = 3

# Limit wagi pliku graficznego (KB), powyżej którego zgłaszamy problem z kompresją.
IMAGE_SIZE_LIMIT_KB = 100
# Ile obrazków sprawdzamy realnie (żądaniami HEAD) - reszta jest pomijana, żeby nie
# wydłużać audytu w nieskończoność na stronach z dziesiątkami zdjęć.
IMAGE_SIZE_CHECK_LIMIT = 8

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

# Walidacji atrybutu ALT podlegają wyłącznie standardowe rastrowe pliki graficzne.
# Grafiki wektorowe (SVG) są zwykle ikonami/elementami UI, dla których wymóg ALT
# nie ma sensu biznesowego i sztucznie zawyżałby liczbę wykrytych błędów.
RASTER_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_DECORATIVE_HINT_RE = re.compile(r"\bicons?\b|\bplaceholder\b|\bdecorative\b", re.I)

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
        # Ustawiane przez fetch() - liczba przekierowań (301/302) napotkanych po drodze
        # do finalnego URL-a. Domyślnie 0, żeby parse() wywołane samodzielnie (np. w
        # testach, bez wcześniejszego fetch()) miało bezpieczną wartość.
        self._last_redirect_count = 0

    def scrape(self, url: str) -> dict:
        normalized_url = self._normalize_url(url)
        html = self.fetch(normalized_url)
        return self.parse(html, normalized_url)

    def fetch(self, url: str) -> str:
        # Przekierowania obsługujemy ręcznie (follow_redirects=False), bo publiczny adres
        # może przekierować w głąb sieci lokalnej - każdy skok musi przejść tę samą
        # walidację co adres podany przez użytkownika (ochrona przed SSRF).
        try:
            safe_url = validate_public_url(url)
        except UnsafeUrlError as exc:
            raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc

        hops = 0
        try:
            with httpx.Client(
                headers=self.headers, timeout=self.timeout, follow_redirects=False
            ) as client:
                response = client.get(safe_url)
                while response.is_redirect and hops < MAX_REDIRECT_HOPS:
                    next_request = response.next_request
                    if next_request is None:
                        break
                    try:
                        safe_url = validate_public_url(str(next_request.url))
                    except UnsafeUrlError as exc:
                        raise ScraperError(
                            f"Przekierowanie z {url} prowadzi do niedozwolonego adresu: {exc}"
                        ) from exc
                    response = client.get(safe_url)
                    hops += 1
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScraperError(f"Nie udało się pobrać {url}: {exc}") from exc

        # Liczba przekierowań napotkanych po drodze - parse() zgłasza na jej podstawie
        # test "Przekierowania 301/302" (zero dodatkowych zapytań).
        self._last_redirect_count = hops
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

        images = self._analyze_images(soup.find_all("img"), url)
        schema = self._extract_schema(soup)
        page_type = self._detect_page_type(url, soup)
        faq_detected = self._detect_faq_section(soup)
        heading_noise = self._analyze_heading_noise(soup)
        eeat = self._analyze_eeat_signals(soup)
        meta_keywords_present = bool(self._get_meta_content(soup, "keywords"))
        internal_links_count = self._count_internal_links(soup, url)
        js_rendering = self._analyze_js_rendering(soup)

        return {
            "url": url,
            "title": title,
            "title_length": len(title) if title else 0,
            "meta_description": meta_description,
            "meta_description_length": len(meta_description) if meta_description else 0,
            "meta_keywords_present": meta_keywords_present,
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
            "images_checkable_srcs": images["checkable_srcs"],
            "schema": schema,
            "page_type": page_type,
            "faq_detected": faq_detected,
            "heading_noise": heading_noise,
            "eeat": eeat,
            "internal_links_count": internal_links_count,
            "js_rendering": js_rendering,
            "redirect_count": self._last_redirect_count,
        }

    def _get_meta_content(self, soup: BeautifulSoup, name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": name})
        content = tag.get("content", "").strip() if tag else ""
        return content or None

    # ------------------------------------------------------------------
    # Analiza obrazków: ALT, title, ASCII w src
    # ------------------------------------------------------------------
    def _analyze_images(self, images: list, page_url: str = "") -> dict:
        validatable = [img for img in images if self._is_validatable_image(img)]

        with_alt = [img for img in validatable if img.get("alt", "").strip()]
        without_alt = [img for img in validatable if not img.get("alt", "").strip()]
        with_title = [img for img in validatable if img.get("title", "").strip()]
        without_title = [img for img in validatable if not img.get("title", "").strip()]
        non_ascii_src = [
            img["src"] for img in validatable if img.get("src") and not img["src"].isascii()
        ]
        without_alt_src = [img["src"] for img in without_alt if img.get("src")]
        # Bezwzględne adresy próbki obrazków do sprawdzenia wagi pliku (patrz
        # `SEOScraper.check_image_sizes`) - ograniczone do IMAGE_SIZE_CHECK_LIMIT,
        # żeby audyt nie wysyłał dziesiątek żądań HEAD na stronach z wieloma zdjęciami.
        checkable_srcs = [
            urljoin(page_url, img["src"])
            for img in validatable[:IMAGE_SIZE_CHECK_LIMIT]
            if img.get("src")
        ]

        return {
            "total": len(validatable),
            "total_all_images": len(images),
            "skipped_non_raster": len(images) - len(validatable),
            "with_alt": len(with_alt),
            "without_alt": len(without_alt),
            "without_alt_examples": without_alt_src[:10],
            "with_title": len(with_title),
            "without_title": len(without_title),
            "non_ascii_src_count": len(non_ascii_src),
            "non_ascii_src_examples": non_ascii_src[:5],
            "checkable_srcs": checkable_srcs,
        }

    def _is_validatable_image(self, img) -> bool:
        """Czy obrazek podlega walidacji ALT/title - tylko standardowe pliki rastrowe
        (JPG/PNG/WEBP/GIF), z pominięciem SVG oraz dekoracyjnych ikon/elementów UI."""
        src = (img.get("src") or "").strip().lower()
        if not src:
            return False

        path = src.split("?", 1)[0].split("#", 1)[0]
        if not path.endswith(RASTER_IMAGE_EXTENSIONS):
            return False

        if img.get("role") == "presentation" or img.get("aria-hidden") == "true":
            return False

        class_attr = img.get("class") or []
        if isinstance(class_attr, str):
            class_attr = class_attr.split()
        if _DECORATIVE_HINT_RE.search(" ".join(class_attr)):
            return False
        if _DECORATIVE_HINT_RE.search(img.get("id") or ""):
            return False

        return True

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

    # ------------------------------------------------------------------
    # Linkowanie wewnętrzne
    # ------------------------------------------------------------------
    def _count_internal_links(self, soup: BeautifulSoup, url: str) -> int:
        """Liczy odnośniki <a href> prowadzące do tej samej domeny (lub adresy
        względne) - pomija kotwice (#), mailto:, tel: i javascript:."""
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[len("www."):]

        count = 0
        for tag in soup.find_all("a", href=True):
            if self._is_internal_link(tag["href"], domain):
                count += 1
        return count

    def _is_internal_link(self, href: str, domain: str) -> bool:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return False

        netloc = urlparse(href).netloc.lower()
        if not netloc:
            return True  # adres względny -> ta sama domena

        if netloc.startswith("www."):
            netloc = netloc[len("www."):]
        return netloc == domain

    # ------------------------------------------------------------------
    # Renderowanie JavaScript (heurystyka SSR vs CSR)
    # ------------------------------------------------------------------
    def _analyze_js_rendering(self, soup: BeautifulSoup) -> dict:
        """Heurystyka SSR vs CSR: liczy widoczny tekst strony (bez kodu <script>/
        <style>) i zestawia go z liczbą znaczników <script>. Bardzo mało tekstu przy
        wielu skryptach (typowy wzorzec pustego <div id="root">/"app"> wypełnianego
        dopiero przez JS w przeglądarce) sugeruje renderowanie wyłącznie po stronie
        klienta (CSR) - taka treść jest niewidoczna dla części robotów wyszukiwarek
        i modeli LLM, które nie wykonują JavaScriptu."""
        script_count = len(soup.find_all("script"))

        # Kopia niezależna od `soup` używanego przez resztę parse() - decompose()
        # nieodwracalnie usuwa węzły, więc operujemy na osobnym drzewie.
        text_only = copy.deepcopy(soup)
        for tag in text_only(["script", "style", "noscript"]):
            tag.decompose()
        visible_text = text_only.get_text(separator=" ", strip=True)
        word_count = len(visible_text.split())

        likely_csr = word_count < JS_CSR_WORD_COUNT_THRESHOLD and script_count >= JS_CSR_MIN_SCRIPT_COUNT
        return {
            "word_count": word_count,
            "script_count": script_count,
            "likely_csr": likely_csr,
        }

    # ------------------------------------------------------------------
    # Dodatkowe, w pełni opcjonalne sprawdzenia sieciowe - każde jest wywoływane
    # osobno (patrz AuditService.run_audit) i niezależnie zabezpieczone: błąd
    # pojedynczego sprawdzenia (timeout, 404, brak nagłówka) nigdy nie przerywa
    # audytu ani nie wpływa na pozostałe sprawdzenia.
    # ------------------------------------------------------------------
    def check_robots_txt(self, base_url: str) -> dict:
        """Sprawdza obecność i podstawową treść pliku /robots.txt pod audytowaną
        domeną - osobne, krótkie zapytanie GET."""
        robots_url = self._build_absolute_url(base_url, "/robots.txt")
        try:
            response = httpx.get(
                validate_public_url(robots_url), headers=self.headers, timeout=min(self.timeout, 10.0)
            )
        except (httpx.HTTPError, UnsafeUrlError):
            return {"checked": True, "exists": False, "disallows_all": False}

        if response.status_code != 200:
            return {"checked": True, "exists": False, "disallows_all": False}

        return {
            "checked": True,
            "exists": True,
            "disallows_all": self._robots_disallows_everything(response.text),
        }

    def _robots_disallows_everything(self, content: str) -> bool:
        """Czy robots.txt blokuje CAŁĄ witrynę dla wszystkich robotów
        ("User-agent: *" + "Disallow: /") - typowy, poważny błąd konfiguracji."""
        wildcard_user_agent = False
        for raw_line in content.splitlines():
            line = raw_line.strip().lower()
            if line.startswith("user-agent:"):
                wildcard_user_agent = line.split(":", 1)[1].strip() == "*"
            elif wildcard_user_agent and line.startswith("disallow:"):
                if line.split(":", 1)[1].strip() == "/":
                    return True
        return False

    def check_custom_404_page(self, base_url: str) -> dict:
        """Odpytuje jawnie nieistniejący adres pod audytowaną domeną, żeby sprawdzić,
        czy serwer poprawnie zwraca kod 404 (a nie "miękkie 404" - status 200 z
        generyczną stroną, mylące dla robotów indeksujących)."""
        probe_path = f"/seo-auditor-404-check-{uuid4().hex[:10]}"
        probe_url = self._build_absolute_url(base_url, probe_path)
        try:
            response = httpx.get(
                validate_public_url(probe_url),
                headers=self.headers,
                timeout=min(self.timeout, 10.0),
                follow_redirects=True,
            )
        except (httpx.HTTPError, UnsafeUrlError):
            return {"checked": False, "returns_404": False, "status_code": None}

        return {
            "checked": True,
            "returns_404": response.status_code == 404,
            "status_code": response.status_code,
        }

    def check_image_sizes(self, image_urls: list[str]) -> dict:
        """Sprawdza wagę (KB) próbki obrazków przez żądania HEAD (bez pobierania
        całej zawartości pliku). Obrazki bez nagłówka Content-Length lub z
        nieudanym żądaniem są pomijane - nie liczą się ani jako "OK", ani jako
        "zbyt ciężkie". Błąd pojedynczego obrazka nigdy nie przerywa sprawdzenia
        pozostałych."""
        oversized = []
        checked_count = 0
        for image_url in image_urls:
            try:
                # Adresy obrazków pochodzą z audytowanej (obcej) strony, więc są danymi
                # niezaufanymi - <img src="http://127.0.0.1:8000/..."> to najprostsza
                # droga do SSRF, jeśli nie sprawdzić ich tak samo jak adresu audytu.
                response = httpx.head(
                    validate_public_url(image_url),
                    headers=self.headers,
                    timeout=min(self.timeout, 8.0),
                    follow_redirects=True,
                )
                content_length = response.headers.get("content-length")
                if content_length is None:
                    continue
                size_kb = int(content_length) / 1024
            except (httpx.HTTPError, UnsafeUrlError, ValueError):
                continue

            checked_count += 1
            if size_kb > IMAGE_SIZE_LIMIT_KB:
                oversized.append({"src": image_url, "size_kb": round(size_kb)})

        return {"checked_count": checked_count, "oversized": oversized}

    def _build_absolute_url(self, base_url: str, path: str) -> str:
        parsed = urlparse(base_url)
        return f"{parsed.scheme}://{parsed.netloc}{path}"
