"""Warstwa prezentacji audytu: etykiety, definicje i przekształcenia metryk.

Wydzielone z `auditor.views`, który urósł do ~700 linii, z czego ponad połowę stanowiły
słowniki etykiet i funkcje przekształcające metryki na struktury dla szablonów. Widoki
odpowiadają teraz wyłącznie za obsługę żądania, a ten moduł - za to, jak dane audytu są
opisane i pogrupowane w interfejsie. Funkcje są czystymi przekształceniami (bez I/O),
więc dają się testować bez klienta HTTP.
"""
from __future__ import annotations

import re

from auditor.models import AuditMetric
from auditor.services.audit_service import SCORE_WEIGHTS

STRATEGY_LABELS = {"mobile": "📱 ", "desktop": "🖥️ "}

# Czytelne etykiety kategorii - używane w tabeli "Problemy i rekomendacje" oraz w PDF.
CATEGORY_LABELS = {
    "seo": "Meta Tagi",
    "technical": "SEO Techniczne",
    "performance": "Szybkość strony",
    "structure": "Schema.org / GEO",
}

# Etykiety i kolejność kategorii na paskach postępu w panelu "Przegląd".
OVERVIEW_CATEGORY_ORDER = [
    ("seo", "SEO On-Page"),
    ("technical", "SEO Techniczne"),
    ("performance", "Szybkość i Wydajność"),
    ("structure", "Dane Strukturalne & LLM"),
]

# Wyodrębnia bloki kodu ```...``` (opcjonalnie z nazwą języka) z tekstu rekomendacji AI.
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z]*\n?(.*?)```", re.DOTALL)

# Ikony kategorii wyświetlane w nagłówku karty metryki.
CATEGORY_ICONS = {
    "seo": "🏷️",
    "technical": "⚙️",
    "performance": "⚡",
    "structure": "🧬",
}

# Krótkie, biznesowe wyjaśnienia metryk ("Co to jest?") wyświetlane na kartach metryk -
# tłumaczą nietechnicznemu odbiorcy, czym jest dana metryka i dlaczego ma znaczenie dla SEO.
METRIC_DEFINITIONS = {
    "title": "Znacznik <title> to tytuł strony widoczny w wynikach wyszukiwania Google oraz na karcie przeglądarki. To jeden z najważniejszych sygnałów SEO — musi być unikalny, zawierać słowa kluczowe i mieścić się w limicie ok. 60-65 znaków, by nie zostać obcięty.",
    "meta_description": "Meta opis to krótki fragment tekstu wyświetlany pod tytułem strony w wynikach wyszukiwania. Nie wpływa bezpośrednio na ranking, ale decyduje o tym, czy użytkownik kliknie w wynik (CTR) — dobrze napisany opis realnie zwiększa liczbę odwiedzin.",
    "h1_structure": "Nagłówek H1 to główny tytuł treści na stronie, informujący zarówno użytkownika, jak i roboty wyszukiwarek, czego dotyczy dana podstrona. Strona powinna mieć dokładnie jeden H1, spójny tematycznie z tytułem i treścią.",
    "canonical": "Znacznik canonical wskazuje wyszukiwarce, która wersja adresu URL jest tą \"oryginalną\", gdy ta sama treść dostępna jest pod wieloma adresami. Brak lub błędny canonical może prowadzić do rozproszenia mocy SEO między duplikaty i problemów z indeksacją.",
    "open_graph": "Znaczniki Open Graph (og:title, og:description, og:image) kontrolują, jak strona wygląda po udostępnieniu w mediach społecznościowych (Facebook, LinkedIn). Ich brak sprawia, że udostępniony link wygląda nieprofesjonalnie i zniechęca do kliknięcia.",
    "images_alt": "Atrybut ALT to tekstowy opis obrazka, odczytywany przez czytniki ekranu i roboty wyszukiwarek, które nie \"widzą\" grafik. Brak atrybutu ALT na zdjęciach treściowych to problem dostępności (accessibility) oraz utracona szansa na ruch z wyszukiwania grafiki.",
    "schema_page_type": "Dane strukturalne Schema.org (JSON-LD) informują wyszukiwarki i modele AI, jakim typem treści jest strona (np. Artykuł, Produkt, Firma lokalna). Poprawnie oznaczony typ strony zwiększa szansę na bogate wyniki wyszukiwania (rich snippets) i widoczność w AI Overviews.",
    "schema_breadcrumbs": "Znacznik BreadcrumbList opisuje ścieżkę nawigacyjną strony (np. Strona główna > Kategoria > Produkt) w formacie zrozumiałym dla wyszukiwarek. Umożliwia wyświetlenie czytelnej okruszkowej nawigacji bezpośrednio w wynikach Google zamiast surowego adresu URL.",
    "schema_faq": "Znacznik FAQPage pozwala oznaczyć sekcję pytań i odpowiedzi na stronie tak, by wyszukiwarka mogła wyświetlić je bezpośrednio w wynikach wyszukiwania jako rozwijaną listę. To zwiększa zajmowaną powierzchnię w SERP i poprawia widoczność w wynikach generowanych przez AI.",
    "heading_order": "Poprawna hierarchia nagłówków (H1 → H2 → H3, bez przeskakiwania poziomów) pomaga zarówno użytkownikom, jak i robotom wyszukiwarek zrozumieć strukturę logiczną treści. Chaotyczna kolejność nagłówków utrudnia indeksację i obniża czytelność strony.",
    "heading_noise": "Nagłówki powinny zawierać rzeczywistą treść merytoryczną, a nie elementy interfejsu (np. \"Menu\", \"Szukaj\", \"Kliknij tutaj\"). Nadużywanie znaczników nagłówkowych do celów wizualnych rozmywa sygnał tematyczny strony dla wyszukiwarek.",
    "image_quality": "Metryka ocenia techniczną jakość obrazków na stronie (m.in. wagę plików i format). Zbyt duże, nieoptymalne grafiki spowalniają wczytywanie strony, co bezpośrednio pogarsza wskaźniki Core Web Vitals i doświadczenie użytkownika.",
    "eeat_authorship": "Sygnały E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness) to elementy budujące wiarygodność treści w oczach Google, takie jak widoczna informacja o autorze. Ich obecność jest szczególnie istotna dla treści eksperckich (YMYL) oraz widoczności w wynikach generowanych przez AI.",
    "eeat_freshness": "Data publikacji lub ostatniej aktualizacji treści to sygnał świeżości, który wyszukiwarki biorą pod uwagę przy ocenie aktualności i wiarygodności strony. Jej brak utrudnia ocenę, czy treść nadal odzwierciedla aktualny stan wiedzy.",
    "pagespeed_score": "Ogólny wynik PageSpeed (0-100) to zbiorcza ocena wydajności strony wystawiana przez Google na podstawie kluczowych wskaźników ładowania i interaktywności. Wyższy wynik przekłada się na lepsze wrażenia użytkownika i jest jednym z sygnałów rankingowych Google.",
    "lcp": "LCP (Largest Contentful Paint) mierzy czas, po którym największy widoczny element strony (np. baner, nagłówek) w pełni się wyrenderuje. To kluczowy wskaźnik postrzeganej szybkości ładowania — powinien wynosić poniżej 2,5 sekundy.",
    "cls": "CLS (Cumulative Layout Shift) mierzy, jak bardzo elementy strony \"skaczą\" podczas ładowania (np. przez obrazki bez zarezerwowanego miejsca). Wysoki CLS frustruje użytkowników i jest karany przez Google jako zły sygnał doświadczenia strony.",
    "fcp": "FCP (First Contentful Paint) mierzy czas, po którym na ekranie pojawia się pierwszy element treści (tekst, obraz). Krótszy czas FCP oznacza, że użytkownik szybciej widzi oznaki ładowania się strony, zamiast pustego ekranu.",
    "inp": "INP (Interaction to Next Paint) mierzy responsywność strony na działania użytkownika (np. kliknięcie przycisku) przez cały czas wizyty. Wysoki INP oznacza, że interfejs \"zawiesza się\" lub reaguje z opóźnieniem, co pogarsza doświadczenie użytkownika.",
    "meta_keywords": "Znacznik <meta name=\"keywords\"> był używany przez wyszukiwarki 20 lat temu - dziś Google go całkowicie ignoruje przy rankingu, a jego obecność jedynie niepotrzebnie ujawnia konkurencji listę słów kluczowych, na które celuje strona.",
    "internal_linking": "Linki wewnętrzne (prowadzące do innych podstron tej samej witryny) pomagają robotom wyszukiwarek odkrywać i rozumieć hierarchię treści serwisu, a użytkownikom - poruszać się po nim. Zbyt mało linków wewnętrznych utrudnia indeksację głębszych podstron.",
    "javascript_rendering": "Część silników JavaScript (React, Vue, Angular) generuje treść strony dopiero w przeglądarce (CSR - Client-Side Rendering). Jeśli surowy HTML nie zawiera realnej treści, część robotów wyszukiwarek i modeli LLM, które nie wykonują JS, zobaczy pustą stronę.",
    "redirect_chain": "Przekierowania 301/302 kierują użytkownika i roboty z jednego adresu URL na inny (np. z http na https). Zbyt długi łańcuch kolejnych przekierowań spowalnia ładowanie strony i marnuje tzw. \"budżet indeksowania\" (crawl budget) wyszukiwarki.",
    "robots_txt": "Plik /robots.txt informuje roboty wyszukiwarek, które fragmenty witryny mogą, a których nie powinny odwiedzać. Błędna konfiguracja (np. zablokowanie całej witryny) może całkowicie wyłączyć stronę z indeksu Google.",
    "http_errors": "Gdy użytkownik lub robot trafi na nieistniejący adres, serwer powinien zwrócić kod HTTP 404 (\"nie znaleziono\"). Zwracanie w takiej sytuacji kodu 200 (tzw. \"miękkie 404\") myli roboty wyszukiwarek co do tego, które adresy naprawdę istnieją.",
    "image_compression": "Zbyt ciężkie pliki graficzne (powyżej ok. 100 KB) wydłużają czas ładowania strony, co bezpośrednio pogarsza wskaźniki Core Web Vitals (zwłaszcza LCP) oraz doświadczenie użytkownika na wolniejszych połączeniach mobilnych.",
    "ssl_certificate": "Certyfikat SSL (protokół HTTPS) szyfruje połączenie między przeglądarką a serwerem. Jego brak jest oznaczany przez przeglądarki jako \"niebezpieczne\", odstrasza użytkowników i jest sygnałem rankingowym branym pod uwagę przez Google.",
}

# "Oficjalne" nazwy testów zgodne ze standardem pełnego Audytu SEO (AI Ready) -
# nadpisują domyślną, automatycznie generowaną etykietę metryki (patrz
# `annotate_metric_labels`). Kilka technicznych kluczy dzieli tę samą oficjalną
# nazwę, gdy audyt opisuje je jako jeden łączny test.
OFFICIAL_TEST_NAMES = {
    "heading_order": "Struktura nagłówków Hx i oczyszczenie z szumu nawigacyjnego",
    "heading_noise": "Struktura nagłówków Hx i oczyszczenie z szumu nawigacyjnego",
    "title": "Optymalizacja znaczników Title i Description",
    "meta_description": "Optymalizacja znaczników Title i Description",
    "meta_keywords": "Weryfikacja obecności zbędnych Meta Keywords",
    "images_alt": "Atrybuty ALT oraz tytuły plików graficznych",
    "image_quality": "Atrybuty ALT oraz tytuły plików graficznych",
    "image_compression": "Wielkość i kompresja plików graficznych (>100KB)",
    "eeat_authorship": "Weryfikacja semantyki treści i zgodności z EEAT+ (Autor, Live Update Badge)",
    "eeat_freshness": "Weryfikacja semantyki treści i zgodności z EEAT+ (Autor, Live Update Badge)",
    "schema_page_type": "Dane strukturalne Schema.org (Organization, Course, School, FAQPage)",
    "schema_breadcrumbs": "Dane strukturalne Schema.org (Organization, Course, School, FAQPage)",
    "schema_faq": "Dane strukturalne Schema.org (Organization, Course, School, FAQPage)",
    "javascript_rendering": "Renderowanie treści JavaScript (SSR vs CSR) w kontekście AI/LLM",
    "internal_linking": "Linkowanie wewnętrzne i architektura nawigacji",
    "http_errors": "Obsługa błędów 4xx/5xx i dedykowana strona 404",
    "lcp": "Wskaźniki Core Web Vitals i szybkość ładowania (LCP, CLS)",
    "cls": "Wskaźniki Core Web Vitals i szybkość ładowania (LCP, CLS)",
}


# ----------------------------------------------------------------------
# Dedykowana tabela statusów Schema.org (zakładka Audyt Techniczny, akordeon
# "Dane Strukturalne") - wyciągnięta z ogólnej listy testów, patrz
# `build_schema_status_table`.
# ----------------------------------------------------------------------
SCHEMA_STATUS_ICONS = {"detected": "🟢", "recommended": "🟡", "not_applicable": "⚪"}
SCHEMA_STATUS_LABELS = {
    "detected": "Wykryto",
    "recommended": "Brak (Rekomendowane)",
    "not_applicable": "Nie dotyczy",
}

# (zbiór typów Schema.org, nazwa wyświetlana, opis, zbiór page_type dla których jest
# rekomendowany | None = zawsze rekomendowany | "faq" = zależnie od wykrycia sekcji FAQ).
SCHEMA_TABLE_DEFINITIONS = [
    ({"Organization", "LocalBusiness"}, "Organization / LocalBusiness", "Dane firmowe i kontaktowe", {"homepage"}),
    ({"WebPage"}, "WebPage", "Kontekst podstrony", None),
    ({"BreadcrumbList"}, "BreadcrumbList", "Nawigacja okruszkowa", {"product", "article", "category", "generic"}),
    ({"Course"}, "Course", "Podstrony kursów i grup wiekowych", set()),
    ({"School"}, "School", "Podstrony placówek i filii lokalnych", set()),
    ({"FAQPage"}, "FAQPage", "Sekcje pytań i odpowiedzi", "faq"),
    ({"Article", "BlogPosting"}, "Article", "Wpisy blogowe", {"article"}),
]


def build_schema_status_table(metrics: list[AuditMetric]) -> list[dict]:
    """Buduje dedykowaną tabelę statusów Schema.org (SCHEMA_TABLE_DEFINITIONS) na
    podstawie danych już zebranych w metrykach `schema_page_type`/`schema_faq` -
    bez ponownego odpytywania strony. Każdy typ dostaje jeden z trzech statusów:
    "detected" (wykryto), "recommended" (brak, ale rekomendowany dla tego typu
    podstrony) albo "not_applicable" (nie dotyczy tej podstrony)."""
    schema_metric = next((m for m in metrics if m.short_key == "schema_page_type"), None)
    faq_metric = next((m for m in metrics if m.short_key == "schema_faq"), None)

    types_found = set(schema_metric.value.get("types_found", [])) if schema_metric else set()
    page_type = schema_metric.value.get("page_type", "generic") if schema_metric else "generic"
    faq_detected = bool(faq_metric.value.get("faq_detected")) if faq_metric else False

    rows = []
    for type_names, display_name, description, applicability in SCHEMA_TABLE_DEFINITIONS:
        if type_names & types_found:
            status = "detected"
        elif applicability == "faq":
            status = "recommended" if faq_detected else "not_applicable"
        elif applicability is None or (applicability and page_type in applicability):
            status = "recommended"
        else:
            status = "not_applicable"

        rows.append({
            "name": display_name,
            "description": description,
            "status": status,
            "status_icon": SCHEMA_STATUS_ICONS[status],
            "status_label": SCHEMA_STATUS_LABELS[status],
        })
    return rows



# ----------------------------------------------------------------------
# Grupowanie testów w 4 akordeony zakładki "Audyt Techniczny" (detail.html).
# Klucze Schema.org (schema_page_type/schema_breadcrumbs/schema_faq) są celowo
# wyłączone z tego grupowania - mają własną, dedykowaną tabelę (patrz wyżej).
# ----------------------------------------------------------------------
SCHEMA_METRIC_KEYS = {"schema_page_type", "schema_breadcrumbs", "schema_faq"}

TECHNICAL_ACCORDIONS = [
    (
        "indexing",
        "🌐 Indeksacja, Renderowanie & Nawigacja",
        {"javascript_rendering", "robots_txt", "canonical", "redirect_chain", "internal_linking", "http_errors"},
    ),
    (
        "content",
        "✍️ Meta Tagi, Treść & EEAT+",
        {
            "title", "meta_description", "meta_keywords", "open_graph", "h1_structure",
            "heading_order", "heading_noise", "eeat_authorship", "eeat_freshness",
        },
    ),
    (
        "images_performance",
        "⚡ Obrazy, Wydajność & Bezpieczeństwo",
        {"images_alt", "image_quality", "image_compression", "ssl_certificate",
         "pagespeed_score", "lcp", "cls", "fcp", "inp"},
    ),
]


STATUS_SORT_PRIORITY = {
    AuditMetric.MetricStatus.ERROR: 0,
    AuditMetric.MetricStatus.WARNING: 1,
    AuditMetric.MetricStatus.OK: 2,
    AuditMetric.MetricStatus.INFO: 2,
}


def group_technical_accordions(metrics: list[AuditMetric]) -> list[dict]:
    """Grupuje metryki (poza Schema.org) w 4 tematyczne akordeony zakładki "Audyt
    Techniczny" wg tematu, a w obrębie każdego akordeonu sortuje je wg priorytetu
    statusu - błędy i ostrzeżenia (wymagające uwagi) na górze, zdane/opcjonalne
    testy na dole (patrz STATUS_SORT_PRIORITY) - żeby najważniejsze problemy były
    widoczne bez przewijania."""
    groups = {group_id: [] for group_id, _, _ in TECHNICAL_ACCORDIONS}
    for metric in metrics:
        if metric.short_key in SCHEMA_METRIC_KEYS:
            continue
        for group_id, _, keys in TECHNICAL_ACCORDIONS:
            if metric.short_key in keys:
                groups[group_id].append(metric)
                break
    return [
        {
            "id": group_id,
            "label": label,
            "metrics": sorted(groups[group_id], key=lambda m: STATUS_SORT_PRIORITY.get(m.status, 3)),
        }
        for group_id, label, _ in TECHNICAL_ACCORDIONS
    ]


def split_recommendation_segments(recommendation: str | None) -> list[dict]:
    """Dzieli tekst rekomendacji AI na segmenty tekstowe i bloki kodu (```...```),
    żeby móc wyrenderować kod w podświetlanej ramce z przyciskiem "Kopiuj"."""
    if not recommendation:
        return []

    segments = []
    last_end = 0
    for match in _CODE_FENCE_RE.finditer(recommendation):
        if match.start() > last_end:
            text = recommendation[last_end:match.start()].strip()
            if text:
                segments.append({"type": "text", "content": text})
        code = match.group(1).strip()
        if code:
            segments.append({"type": "code", "content": code})
        last_end = match.end()

    remaining = recommendation[last_end:].strip()
    if remaining:
        segments.append({"type": "text", "content": remaining})

    if not segments and recommendation.strip():
        segments.append({"type": "text", "content": recommendation.strip()})

    return segments


def annotate_metric_labels(metrics: list[AuditMetric]) -> list[AuditMetric]:
    """Dolicza do każdej metryki `short_key` (klucz bez przedrostka strategii),
    `display_key` (czytelna etykieta), `category_label` oraz `recommendation_segments`
    (rekomendacja AI podzielona na tekst/bloki kodu do prezentacji w raporcie)."""
    for metric in metrics:
        short_key = metric.key
        strategy_emoji = ""
        for strategy, emoji in STRATEGY_LABELS.items():
            prefix = f"{strategy}_"
            if metric.key.startswith(prefix):
                short_key = metric.key[len(prefix):]
                strategy_emoji = emoji
                break
        metric.short_key = short_key
        label = metric.value.get("label") if isinstance(metric.value, dict) else None
        official_name = OFFICIAL_TEST_NAMES.get(short_key)
        metric.display_key = f"{strategy_emoji}{official_name or label or short_key.replace('_', ' ')}"
        metric.category_label = CATEGORY_LABELS.get(metric.category, metric.category)
        metric.category_icon = CATEGORY_ICONS.get(metric.category, "🔎")
        metric.definition = METRIC_DEFINITIONS.get(short_key, "")

        recommendation = metric.value.get("recommendation") if isinstance(metric.value, dict) else None
        metric.recommendation_segments = split_recommendation_segments(recommendation)
    return metrics


# Metryki score per urządzenie zastąpione są w podsumowaniu (Priorytety/Ostrzeżenia)


# jedną zbiorczą metryką "pagespeed_score" - nie pokazujemy ich tam osobno.
MERGED_PAGESPEED_SCORE_KEYS = {"mobile_pagespeed_score", "desktop_pagespeed_score"}


# ----------------------------------------------------------------------
# Generator drukowalnego raportu PDF (auditor:download_pdf_report)
# ----------------------------------------------------------------------

# Zespół odpowiedzialny za wdrożenie poprawki, wg kategorii metryki.
TEAM_BY_CATEGORY = {
    "seo": "SEO",
    "technical": "IT",
    "performance": "IT",
    "structure": "IT",
}

# Bazowy priorytet (1-10) wg statusu oraz dodatkowy "boost" wg kategorii -
# wydajność i dane strukturalne mają największy wpływ na widoczność w Google/LLM,
# więc przy tym samym statusie trafiają wyżej na liście wdrożeniowej.
PRIORITY_BASE_BY_STATUS = {
    AuditMetric.MetricStatus.ERROR: 7,
    AuditMetric.MetricStatus.WARNING: 4,
}
PRIORITY_BOOST_BY_CATEGORY = {"performance": 3, "structure": 2, "seo": 1, "technical": 1}


def priority_for_metric(metric: AuditMetric) -> int:
    base = PRIORITY_BASE_BY_STATUS.get(metric.status, 3)
    boost = PRIORITY_BOOST_BY_CATEGORY.get(metric.category, 0)
    return min(10, base + boost)


def compute_category_scores(metrics: list[AuditMetric]) -> list[dict]:
    """Liczy % wyniku dla każdej z 4 kategorii (paski postępu w panelu "Przegląd").

    Działa na metrykach JUŻ wczytanych do pamięci - wcześniejsza wersja wykonywała
    `audit.metrics.filter(category=...)` w pętli, czyli 4 dodatkowe zapytania do bazy
    po dane, które widok i tak miał już pobrane.
    """
    by_category: dict[str, list[AuditMetric]] = {}
    for metric in metrics:
        by_category.setdefault(metric.category, []).append(metric)

    results = []
    for category, label in OVERVIEW_CATEGORY_ORDER:
        category_metrics = by_category.get(category, [])
        if category_metrics:
            total = sum(SCORE_WEIGHTS[m.status] for m in category_metrics)
            score = round(total / len(category_metrics))
        else:
            score = 0
        results.append({"label": label, "score": score})
    return results


def score_bucket(score: int) -> str:
    """Zamienia wynik liczbowy na klasę koloru pierścienia oceny (ok/warning/error)."""
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warning"
    return "error"
