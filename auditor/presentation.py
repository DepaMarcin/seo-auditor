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
from auditor.services.audit_service import SCORE_WEIGHTS, SCORED_STATUSES

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

# Nagłówki sekcji rekomendacji ("### 1. CO TO JEST?"). Dopuszczamy 2-4 krzyżyki,
# bo model bywa niekonsekwentny w poziomie nagłówka, a dla nas liczy się sam fakt,
# że to nagłówek sekcji, a nie treść.
_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$", re.MULTILINE)

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
    "bot_accessibility": "Porównanie tego, co widzi prosty robot (surowy kod HTML, bez wykonywania JavaScriptu), z tym, co widzi użytkownik i pełny Googlebot (DOM po wykonaniu skryptów). Gdy treść powstaje dopiero w przeglądarce albo serwer odrzuca automaty, strona jest niewidoczna dla crawlerów i agentów AI - a wszystkie testy struktury treści zwracają wtedy wyniki nieprawdziwe.",
    "schema_entity_linking": "Encje w grafie JSON-LD powinny wskazywać na siebie referencjami @id zamiast powielać pełne definicje. Modele językowe budują odpowiedzi z relacji między encjami - graf, w którym każda strona opisuje firmę od nowa, jest dla modelu zbiorem luźnych obiektów zamiast spójnego opisu biznesu.",
    "price_discrepancy": "Cena w danych strukturalnych musi odpowiadać cenie widocznej na stronie. Gdy do Schema trafia cena hurtowa pobrana wprost z bazy, wyszukiwarka i modele AI obiecują użytkownikowi kwotę, której na stronie nie znajdzie - poza utratą zaufania grozi to karą za niezgodne dane.",
    "schema_data_hygiene": "Dane strukturalne są generowane maszynowo i nikt ich nie ogląda, więc błędy potrafią żyć miesiącami: adresy środowiska testowego, podwójnie zakodowane encje HTML czy nazwa domeny doklejona do nazwy produktu zanieczyszczają to, co wyszukiwarka wie o firmie.",
    "ecommerce_completeness": "Karta produktu bez oceny, marki, danych wysyłki i polityki zwrotów nie kwalifikuje się do rozszerzonych wyników Google i rzadko trafia do odpowiedzi AI - modelowi brakuje informacji, o które pyta użytkownik (koszt wysyłki, możliwość zwrotu).",
    "authorship_depth": "Google ocenia doświadczenie i eksperckość autora po tym, czy da się ustalić jego kompetencje. Samo imię i nazwisko nie wystarcza - potrzebne jest stanowisko lub opis oraz powiązanie z profilem zewnętrznym (sameAs), które łączy autora z dorobkiem poza witryną.",
    "freshness_decay": "Identyczne daty publikacji i modyfikacji po ponad roku oznaczają, że nikt nie zweryfikował, czy treść jest nadal aktualna. Przegląd merytoryczny i odświeżenie daty to sygnał wiarygodności, szczególnie istotny w tematach, w których wiedza szybko się dezaktualizuje.",
    "external_sources": "Treść poradnikowa bez odwołań do źródeł zewnętrznych jest dla wyszukiwarki twierdzeniem bez pokrycia. Linki do domen rządowych, edukacyjnych i uznanych publikacji świadczą, że autor opiera się na zewnętrznej wiedzy, a nie wyłącznie na własnej opinii.",
    "hidden_content": "Crawlery i modele językowe czytają tekst z kodu strony, ale treść ukryta stylem CSS jest traktowana jako mniej istotna lub pomijana. Opinie klientów i FAQ zwinięte pod przyciskiem „pokaż więcej” to zwykle najcenniejszy materiał dla AI - i dokładnie ten, który znika.",
    "heading_visibility": "Nagłówek obecny w kodzie, ale niewidoczny w interfejsie, tworzy dla robota poziom hierarchii, który dla użytkownika nie istnieje. Szczególnie mylące są ukryte komunikaty typu „Nie znaleziono produktów” - robot odczytuje je jako treść strony.",
    "schema_html_parity": "Dane strukturalne muszą mieć pokrycie w treści widocznej na stronie. Deklaracja FAQPage bez sekcji pytań w kodzie albo Product bez ceny to dla Google dane niezgodne z zawartością, za co grozi ręczna kara - a model językowy i tak nie znajdzie obiecanego materiału.",
    "placeholder_content": "Teksty zastępcze („lorem ipsum”, „TODO”, „opis w przygotowaniu”) pozostawione na produkcji są indeksowane jak każda inna treść, a modele językowe mogą je zacytować jako opis oferty.",
    "answer_first": "Wzorzec \"Answer-First\" to zwięzła, bezpośrednia odpowiedź (20-40 słów) umieszczona zaraz pod nagłówkiem sekcji, przed rozbudowanym wyjaśnieniem. Modele językowe cytują właśnie takie fragmenty - sekcja zaczynająca się od długiego wstępu wymaga od modelu samodzielnego streszczenia, co obniża szansę na zacytowanie strony.",
    "structured_content": "Natywne tabele (<table>) i listy (<ul>, <ol>) niosą strukturę wprost, podczas gdy z prozy model musi ją dopiero wywnioskować. Strony z zestawieniami w tabelach i listach są chętniej cytowane w odpowiedziach AI, zwłaszcza przy pytaniach porównawczych i cenowych.",
    "meta_robots": "Znacznik <meta name=\"robots\"> steruje tym, czy wyszukiwarka może zaindeksować daną podstronę i podążać za jej linkami. Dyrektywa \"noindex\" całkowicie wyklucza stronę z wyników wyszukiwania - bywa zostawiona przez pomyłkę po migracji ze środowiska testowego, co jest jedną z najkosztowniejszych usterek SEO.",
    "wayback_domain_age": "Wiek domeny szacowany na podstawie pierwszej migawki w archiwum Internet Archive (Wayback Machine). Domeny z wieloletnią historią cieszą się większym zaufaniem wyszukiwarek, a nowe potrzebują czasu i konsekwentnych publikacji, żeby zbudować autorytet. Archiwum nie jest rejestrem domen, więc data pierwszej migawki to oszacowanie od dołu - domena może być starsza.",
    "robots_ai_bots": "Boty modeli językowych (GPTBot, ClaudeBot, PerplexityBot, Google-Extended, Bytespider) zbierają treść stron, żeby móc ją cytować w odpowiedziach generowanych przez AI. Zablokowanie ich w pliku robots.txt lub nagłówku X-Robots-Tag wyklucza witrynę z tych odpowiedzi — bywa to świadomą decyzją (ochrona treści), ale powinno być wyborem, a nie przypadkiem.",
    "schema_validity": "Dane strukturalne JSON-LD muszą być poprawne składniowo — blok z błędem jest przez wyszukiwarki i modele AI pomijany w całości, tak jakby go nie było. Typy Organization, SoftwareApplication, FAQPage i Product to te, po których AI buduje odpowiedzi o firmie i jej ofercie.",
    "twitter_cards": "Tagi Twitter Card sterują wyglądem linku udostępnionego w serwisie X: typem podglądu, tytułem, opisem i miniaturą. Bez nich (i bez Open Graph) udostępniony link wyświetla się jako goły adres URL, co drastycznie obniża klikalność.",
    "favicon": "Favicon to mała ikona witryny widoczna w karcie przeglądarki, na liście zakładek i w wynikach wyszukiwania na urządzeniach mobilnych. Jej brak sprawia, że strona wygląda niedokończenie i trudniej ją rozpoznać wśród wielu otwartych kart.",
    "thin_content": "\"Thin content\" to strona o zbyt małej objętości treści, żeby wyczerpać temat i konkurować w wynikach wyszukiwania. Google traktuje takie strony jako niskiej wartości, a modele językowe rzadko cytują je jako źródło.",
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
    "schema_entity_linking": "Powiązania encji w grafie Schema.org (@id)",
    "price_discrepancy": "Zgodność ceny w Schema.org z ceną widoczną na stronie",
    "schema_data_hygiene": "Czystość danych strukturalnych (środowiska testowe, kodowanie, sufiksy)",
    "ecommerce_completeness": "Kompletność danych e-commerce dla AI Overviews",
    "authorship_depth": "Sygnały autorstwa E-E-A-T (kompetencje i profil zewnętrzny)",
    "freshness_decay": "Aktualność treści (datePublished vs dateModified)",
    "external_sources": "Powoływanie się na zewnętrzne źródła (weryfikacja faktów)",
    "hidden_content": "Treść ukryta przed modelami językowymi (GEO Suppression)",
    "heading_visibility": "Nagłówki niewidoczne w interfejsie",
    "schema_html_parity": "Zgodność deklaracji Schema.org z treścią HTML",
    "placeholder_content": "Teksty zastępcze pozostawione na produkcji",
    "answer_first": "Wzorzec Answer-First w sekcjach treści (gotowość do cytowania przez AI)",
    "structured_content": "Gęstość elementów ustrukturyzowanych (tabele i listy)",
    "meta_robots": "Dyrektywy indeksacji (meta robots: noindex/nofollow)",
    "wayback_domain_age": "Wiek i historia domeny w archiwum (Wayback Machine)",
    "robots_ai_bots": "Dostęp botów AI/LLM (GPTBot, ClaudeBot, PerplexityBot) w robots.txt",
    "schema_validity": "Poprawność składni JSON-LD i pokrycie typów istotnych dla AI",
    "twitter_cards": "Podgląd linku w mediach społecznościowych (Open Graph i Twitter Card)",
    "favicon": "Ikona witryny (favicon, apple-touch-icon)",
    "bot_accessibility": "Dostępność dla robotów i renderowanie",
    "thin_content": "Objętość treści (thin content)",
    "heading_order": "Hierarchia nagłówków Hx: kolejność, puste nagłówki i przeskoki poziomów",
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
SCHEMA_METRIC_KEYS = {"schema_page_type", "schema_breadcrumbs", "schema_faq", "schema_validity"}

# Metryki Schema.org renderowane jako KARTA testu wewnątrz akordeonu "Dane Strukturalne"
# (obok tabeli pokrycia typów) - w odróżnieniu od pozostałych kluczy SCHEMA_METRIC_KEYS,
# które tabela reprezentuje w całości i osobna karta tylko dublowałaby informację.
SCHEMA_CARD_KEYS = ("schema_validity",)

# Zbiorcza metryka PageSpeed ma własny panel podsumowania na szczycie zakładki
# technicznej (patrz `extract_pagespeed_summary`), dlatego jest CELOWO wyłączona z
# akordeonów tematycznych - inaczej ta sama karta pojawiałaby się dwa razy.
PAGESPEED_SUMMARY_KEY = "pagespeed_score"

TECHNICAL_ACCORDIONS = [
    (
        "indexing",
        "🌐 Indeksacja, Renderowanie & Nawigacja",
        {"javascript_rendering", "robots_txt", "robots_ai_bots", "canonical", "redirect_chain",
         "internal_linking", "http_errors", "favicon", "wayback_domain_age", "meta_robots",
         "bot_accessibility"},
    ),
    (
        "content",
        "✍️ Meta Tagi, Treść & EEAT+",
        {
            "title", "meta_description", "meta_keywords", "open_graph", "twitter_cards",
            "h1_structure", "heading_order", "heading_noise", "thin_content",
            "answer_first", "structured_content", "placeholder_content",
            "eeat_authorship", "eeat_freshness",
        },
    ),
    (
        "images_performance",
        "⚡ Obrazy, Wydajność & Bezpieczeństwo",
        {"images_alt", "image_quality", "image_compression", "ssl_certificate",
         "lcp", "cls", "fcp", "inp"},
    ),
    (
        "geo_ai",
        "🤖 GEO: Dane Strukturalne, E-E-A-T i Dostępność dla AI",
        {
            "schema_entity_linking", "price_discrepancy", "schema_data_hygiene",
            "ecommerce_completeness", "authorship_depth", "freshness_decay",
            "external_sources", "hidden_content", "heading_visibility",
            "schema_html_parity",
        },
    ),
]


STATUS_SORT_PRIORITY = {
    AuditMetric.MetricStatus.ERROR: 0,
    AuditMetric.MetricStatus.WARNING: 1,
    AuditMetric.MetricStatus.OK: 2,
    AuditMetric.MetricStatus.INFO: 2,
    # Na samym dole: test, którego nie dało się przeprowadzić, nie niesie informacji
    # o jakości strony - jego miejsce jest pod wynikami, które ją niosą.
    AuditMetric.MetricStatus.SKIPPED: 3,
}


def group_technical_accordions(metrics: list[AuditMetric]) -> list[dict]:
    """Grupuje metryki (poza Schema.org) w 4 tematyczne akordeony zakładki "Audyt
    Techniczny" wg tematu, a w obrębie każdego akordeonu sortuje je wg priorytetu
    statusu - błędy i ostrzeżenia (wymagające uwagi) na górze, zdane/opcjonalne
    testy na dole (patrz STATUS_SORT_PRIORITY) - żeby najważniejsze problemy były
    widoczne bez przewijania."""
    groups = {group_id: [] for group_id, _, _ in TECHNICAL_ACCORDIONS}
    for metric in metrics:
        if metric.short_key in SCHEMA_METRIC_KEYS or metric.short_key == PAGESPEED_SUMMARY_KEY:
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
    """Dzieli tekst rekomendacji AI na segmenty: nagłówki, akapity i bloki kodu.

    Prompt generatora (auditor.services.rag) wymusza cztery sekcje oznaczone
    `### N. NAZWA`. Bez wydzielenia ich na osobne segmenty znaczniki Markdown
    trafiałyby dosłownie do `<p>` i użytkownik widziałby "### 1. CO TO JEST?".
    Bloki kodu (```...```) idą osobno, żeby wyrenderować je w ramce z przyciskiem
    "Kopiuj".
    """
    if not recommendation:
        return []

    segments = []
    last_end = 0
    for match in _CODE_FENCE_RE.finditer(recommendation):
        if match.start() > last_end:
            segments.extend(_split_headings(recommendation[last_end:match.start()]))
        code = match.group(1).strip()
        if code:
            segments.append({"type": "code", "content": code})
        last_end = match.end()

    segments.extend(_split_headings(recommendation[last_end:]))

    if not segments and recommendation.strip():
        segments.append({"type": "text", "content": recommendation.strip()})

    return segments


def _split_headings(text: str) -> list[dict]:
    """Rozdziela fragment tekstu na nagłówki `### ...` i akapity między nimi."""
    segments = []
    ostatni = 0
    for match in _HEADING_RE.finditer(text):
        akapit = text[ostatni:match.start()].strip()
        if akapit:
            segments.append({"type": "text", "content": akapit})
        segments.append({"type": "heading", "content": match.group(1).strip()})
        ostatni = match.end()

    reszta = text[ostatni:].strip()
    if reszta:
        segments.append({"type": "text", "content": reszta})
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
    """Liczy wynik i rozkład statusów dla każdej z 4 kategorii (karty KPI w "Przegląd").

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
        scored = [m for m in category_metrics if m.status in SCORED_STATUSES]
        if scored:
            total = sum(SCORE_WEIGHTS[m.status] for m in scored)
            score = round(total / len(scored))
        else:
            score = 0
        statuses = [m.status for m in category_metrics]
        results.append(
            {
                "label": label,
                "score": score,
                "key": category,
                "icon": CATEGORY_ICONS.get(category, ""),
                "bucket": score_bucket(score),
                "errors": statuses.count("error"),
                "warnings": statuses.count("warning"),
                "passed": statuses.count("ok"),
                "total": len(category_metrics),
            }
        )
    return results


def score_bucket(score: int) -> str:
    """Zamienia wynik liczbowy na klasę koloru pierścienia oceny (ok/warning/error)."""
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warning"
    return "error"


def extract_schema_cards(metrics: list[AuditMetric]) -> list[AuditMetric]:
    """Metryki Schema.org pokazywane jako karty testu w akordeonie "Dane Strukturalne".

    Wyodrębnione osobno, bo `group_technical_accordions` celowo pomija cały zbiór
    SCHEMA_METRIC_KEYS - inaczej te testy trafiłyby do akordeonów tematycznych,
    z dala od tabeli pokrycia typów, której dotyczą.
    """
    by_key = {m.short_key: m for m in metrics}
    return [by_key[key] for key in SCHEMA_CARD_KEYS if key in by_key]


def extract_pagespeed_summary(metrics: list[AuditMetric]) -> AuditMetric | None:
    """Zbiorcza metryka PageSpeed (Mobile + Desktop) do panelu podsumowania.

    W `value` niesie już `mobile_score`/`desktop_score` (patrz
    `AuditService._build_pagespeed_summary_metric`), więc panel rysuje wskaźniki
    bezpośrednio z metryki - bez dodatkowych pól w kontekście.
    """
    return next((m for m in metrics if m.short_key == PAGESPEED_SUMMARY_KEY), None)


def pagespeed_score_bucket(score: int | None) -> str:
    """Klasa koloru wskaźnika PageSpeed wg progów Google (0-49 / 50-89 / 90-100).

    Świadomie NIE korzysta z `score_bucket` (progi 50/80 dla ogólnej oceny audytu) -
    Google stosuje dla wydajności własne, ostrzejsze granice.
    """
    if score is None:
        return "warning"
    if score >= 90:
        return "ok"
    if score >= 50:
        return "warning"
    return "error"


# ----------------------------------------------------------------------
# Odpowiedzi wyszukiwarki AI (GEO)
# ----------------------------------------------------------------------
# Model odpowiada Markdownem: pogrubienia, listy, linki. Wstawiony do szablonu wprost
# pokazuje użytkownikowi "**[CHEERS]**" i surowe adresy na pół ekranu. Tekst jest przy
# tym NIEZAUFANY - powstaje z treści cytowanych stron - więc najpierw escapujemy
# wszystko, a dopiero potem dokładamy własne znaczniki.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<!href=\")(?<!>)(https?://[^\s<>\"]+)")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_ANSWER_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")

# Adresy w treści skracamy do samej domeny - pełny URL w zdaniu rozbija akapit i nic
# nie wnosi, bo i tak jest klikalny.
_MAX_LABEL_LENGTH = 32


def citation_label(url: str) -> str:
    """Czytelna etykieta źródła: "Orlen.pl" zamiast adresu na pół ekranu."""
    from urllib.parse import urlparse

    host = (urlparse(url or "").hostname or "").removeprefix("www.")
    if not host:
        return (url or "")[:_MAX_LABEL_LENGTH] or "źródło"

    # Pierwsza litera wielka czyta się jak nazwa marki, a nie jak fragment adresu.
    return host[:1].upper() + host[1:]


def _is_safe_url(url: str) -> bool:
    """Tylko http(s) - inaczej `javascript:` z odpowiedzi modelu trafiłby do href."""
    return url.lower().startswith(("http://", "https://"))


def _render_inline(text: str) -> str:
    """Pogrubienia i linki wewnątrz akapitu. Wejście musi być już zescapowane."""
    def link(url: str, label: str) -> str:
        if not _is_safe_url(url):
            return label
        return (
            f'<a class="geo-source-pill" href="{url}" target="_blank" rel="noopener nofollow">'
            f"{label} <span aria-hidden=\"true\">↗</span></a>"
        )

    text = _MD_LINK_RE.sub(lambda m: link(m.group(2), m.group(1)), text)
    text = _BARE_URL_RE.sub(lambda m: link(m.group(1), citation_label(m.group(1))), text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    return text


def render_ai_answer(answer: str | None) -> str:
    """Zamienia odpowiedź modelu na bezpieczny HTML gotowy do wstawienia w szablonie."""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    tekst = (answer or "").strip()
    if not tekst:
        return ""

    bloki: list[str] = []
    punkty: list[str] = []
    akapit: list[str] = []

    def zamknij_akapit() -> None:
        if akapit:
            bloki.append("<p>" + _render_inline(" ".join(akapit)) + "</p>")
            akapit.clear()

    def zamknij_liste() -> None:
        if punkty:
            pozycje = "".join(f"<li>{_render_inline(p)}</li>" for p in punkty)
            bloki.append(f"<ul class=\"geo-answer-list\">{pozycje}</ul>")
            punkty.clear()

    for surowa in escape(tekst).splitlines():
        linia = surowa.strip()

        if not linia:
            zamknij_akapit()
            zamknij_liste()
            continue

        naglowek = _ANSWER_HEADING_RE.match(linia)
        if naglowek:
            zamknij_akapit()
            zamknij_liste()
            bloki.append(
                f'<h4 class="geo-answer-heading">{_render_inline(naglowek.group(1))}</h4>'
            )
            continue

        punkt = _LIST_ITEM_RE.match(linia)
        if punkt:
            zamknij_akapit()
            punkty.append(punkt.group(1))
            continue

        zamknij_liste()
        akapit.append(linia)

    zamknij_akapit()
    zamknij_liste()
    return mark_safe("".join(bloki))


def build_geo_questions(study, queries) -> list[dict]:
    """Pytania z gotowymi do wyświetlenia odpowiedziami i przypisami."""
    pytania = []
    for numer, query in enumerate(queries, start=1):
        przebiegi = []
        for run in query.runs.all():
            przypisy = [
                {
                    "url": c.get("url", ""),
                    "domain": c.get("domain", ""),
                    "label": citation_label(c.get("url", "")),
                    "title": c.get("title", ""),
                    "is_brand": c.get("domain") == study.domain,
                    "safe": _is_safe_url(c.get("url", "")),
                }
                for c in (run.citations or [])
            ]
            przebiegi.append({
                "attempt": run.attempt,
                "brand_cited": run.brand_cited,
                "brand_mentioned": run.brand_mentioned,
                "brand_position": run.brand_position,
                "visibility": run.visibility,
                "visibility_label": run.get_visibility_display(),
                "error": run.error,
                "answer_html": render_ai_answer(run.answer),
                "citations": przypisy,
            })

        pytania.append({
            "pk": query.pk,
            "number": numer,
            "text": query.text,
            "cited_runs": query.cited_runs,
            "citation_rate": query.citation_rate,
            "stability": query.stability,
            "stability_label": query.get_stability_display(),
            "most_common_position": query.most_common_position,
            "competitors": query.competitors or [],
            "mention_runs": query.mention_runs,
            "visible_runs": query.visible_runs,
            "runs": przebiegi,
        })
    return pytania


def build_geo_visibility_totals(queries) -> dict:
    """Ile odpowiedzi zawierało link, samą wzmiankę, a ile nic.

    Rozdzielenie jest istotne: przypis daje ruch, wzmianka bez linku buduje tylko
    rozpoznawalność. Zsumowane w jedną liczbę zacierałyby różnicę, o której klient
    musi wiedzieć, bo prowadzi do innych działań.
    """
    linked = mentions = total = 0
    for query in queries:
        for run in query.runs.all():
            if run.error:
                continue
            total += 1
            if run.brand_cited:
                linked += 1
            elif run.brand_mentioned:
                mentions += 1

    visible = linked + mentions
    return {
        "linked": linked,
        "mentions": mentions,
        "visible": visible,
        "absent": total - visible,
        "total": total,
        "visible_percent": round(visible * 100 / total) if total else 0,
    }


def build_geo_executive_summary(study, queries) -> dict:
    """Dane podsumowania wykonawczego na dole raportu."""
    totals = build_geo_visibility_totals(queries)

    # Konkurenci zliczani przez wszystkie pytania - pojedyncze pytanie pokazuje
    # przypadek, dopiero suma pokazuje, kto naprawdę zajmuje miejsce w odpowiedziach.
    rywale: dict[str, int] = {}
    for query in queries:
        for competitor in query.competitors or []:
            domena = competitor.get("domain")
            if domena and domena != study.domain:
                rywale[domena] = rywale.get(domena, 0) + competitor.get("count", 0)

    top = sorted(rywale.items(), key=lambda pozycja: (-pozycja[1], pozycja[0]))[:3]

    return {
        "domain": study.domain,
        "brand_name": study.brand_name or study.domain,
        "overall_score": study.overall_score,
        "totals": totals,
        "top_competitors": [{"domain": d, "count": c} for d, c in top],
        "top_competitors_list": ", ".join(domena for domena, _ in top),
    }


def build_geo_repetition_stats(study, queries) -> list[dict]:
    """Ile pytań uzyskało cytowanie w kolejnych powtórzeniach.

    Rozbicie po numerze próby pokazuje to, czego nie widać w jednej liczbie: czy
    obecność marki jest powtarzalna, czy model raz ją cytuje, a raz nie.
    """
    if not queries:
        return []

    licznik: dict[int, int] = {}
    suma: dict[int, int] = {}
    for query in queries:
        for run in query.runs.all():
            suma[run.attempt] = suma.get(run.attempt, 0) + 1
            if run.brand_cited:
                licznik[run.attempt] = licznik.get(run.attempt, 0) + 1

    statystyki = []
    for attempt in sorted(suma):
        total = suma[attempt]
        cited = licznik.get(attempt, 0)
        statystyki.append({
            "attempt": attempt,
            "cited": cited,
            "total": total,
            "percent": round(cited * 100 / total) if total else 0,
        })
    return statystyki


def build_geo_sources(study, queries) -> list[dict]:
    """Wszystkie zacytowane domeny z licznikami - tabela źródeł i konkurencji."""
    zebrane: dict[str, dict] = {}
    for query in queries:
        for run in query.runs.all():
            for citation in run.citations or []:
                domena = citation.get("domain") or ""
                if not domena:
                    continue
                wpis = zebrane.setdefault(domena, {
                    "domain": domena,
                    "label": citation_label(citation.get("url", "")),
                    "url": citation.get("url", ""),
                    "count": 0,
                    "question_ids": set(),
                    "is_brand": domena == study.domain,
                    "safe": _is_safe_url(citation.get("url", "")),
                })
                wpis["count"] += 1
                wpis["question_ids"].add(query.pk)

    zrodla = sorted(zebrane.values(), key=lambda w: (-w["count"], w["domain"]))
    for wpis in zrodla:
        pytania = sorted(wpis.pop("question_ids"))
        wpis["question_count"] = len(pytania)
        # Filtr w tabeli porównuje identyfikator pytania z tą listą.
        wpis["question_filter"] = " ".join(f"q{pk}" for pk in pytania)
    return zrodla
