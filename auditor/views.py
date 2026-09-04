import json
import logging
import re
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from google_auth_oauthlib.flow import Flow

from .models import Audit, AuditMetric
from .services.audit_service import SCORE_WEIGHTS, AuditService
from .services.ga4_service import GA4OAuthService

logger = logging.getLogger(__name__)


def index(request):
    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        if not url:
            messages.error(request, "Podaj adres URL do audytu.")
            return redirect("auditor:index")

        audit = Audit.objects.create(url=url)
        AuditService().run_audit(audit)
        return redirect("auditor:detail", pk=audit.pk)

    audits = Audit.objects.all().order_by("-created_at")[:10]
    return render(request, "auditor/index.html", {"audits": audits})


# ----------------------------------------------------------------------
# Google Analytics 4 - integracja OAuth 2.0 ("Zaloguj się przez Google")
# ----------------------------------------------------------------------

def start_ga4_auth(request: HttpRequest, pk: int) -> HttpResponse:
    """Inicjuje przepływ OAuth 2.0 z Google dla danego audytu: buduje `Flow` z pliku
    `client_secret.json`, zapisuje w sesji, którego audytu dotyczy autoryzacja
    (`pending_audit_id`) oraz stan CSRF (`ga4_oauth_state`), po czym przekierowuje
    użytkownika na ekran logowania/zgody Google."""
    audit = get_object_or_404(Audit, pk=pk)

    try:
        flow = Flow.from_client_secrets_file(
            str(settings.GA4_CLIENT_SECRETS_FILE),
            scopes=settings.GA4_SCOPES,
            redirect_uri=settings.GA4_REDIRECT_URI,
        )
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )
    except FileNotFoundError:
        logger.error("Brak pliku client_secret.json (oczekiwana ścieżka: %s).", settings.GA4_CLIENT_SECRETS_FILE)
        messages.error(request, "Konfiguracja Google Analytics jest niekompletna - brak pliku client_secret.json.")
        return redirect("auditor:detail", pk=audit.pk)
    except Exception:
        logger.exception("Nie udało się zainicjować przepływu OAuth Google dla audytu %s.", audit.pk)
        messages.error(request, "Nie udało się rozpocząć logowania przez Google. Spróbuj ponownie.")
        return redirect("auditor:detail", pk=audit.pk)

    request.session["pending_audit_id"] = audit.pk
    request.session["ga4_oauth_state"] = state
    # google-auth-oauthlib generuje PKCE `code_verifier` przy tworzeniu URL-a autoryzacji
    # (flow.authorization_url), ale to inny obiekt `Flow` obsługuje callback (inny request/
    # proces) - bez zapisania go w sesji i odtworzenia w ga4_callback, flow.fetch_token()
    # kończy się błędem "InvalidGrantError: Missing code verifier".
    request.session["code_verifier"] = flow.code_verifier

    return redirect(authorization_url)


def _load_google_client_config() -> tuple[str, str]:
    """Odczytuje `client_id`/`client_secret` bezpośrednio z pliku `client_secret.json`,
    bez budowania pełnego obiektu `Flow` - potrzebne do odtworzenia `Credentials`
    z zapisanego wcześniej `refresh_token` (patrz `_build_credentials_from_refresh_token`)."""
    with open(settings.GA4_CLIENT_SECRETS_FILE, encoding="utf-8") as fh:
        raw_config = json.load(fh)
    config = raw_config.get("web") or raw_config.get("installed") or {}
    return config["client_id"], config["client_secret"]


def _build_credentials_from_refresh_token(audit: Audit):
    """Odtwarza `google.oauth2.credentials.Credentials` z `audit.ga4_refresh_token`,
    żeby móc odpytać GA4 bez ponownego przechodzenia przez ekran zgody Google."""
    client_id, client_secret = _load_google_client_config()
    return GA4OAuthService().build_credentials_from_refresh_token(
        refresh_token=audit.ga4_refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )


def _brand_token(url: str) -> str:
    """Wyciąga rdzeń nazwy domeny (bez schematu, "www." i TLD) do prostego
    dopasowania z nazwą wyświetlaną usługi GA4, np. "https://www.harbingers.io/"
    -> "harbingers", żeby móc podpowiedzieć właściwą usługę na liście wyboru."""
    normalized = url if "://" in url else f"https://{url}"
    domain = urlparse(normalized).netloc.lower()
    if domain.startswith("www."):
        domain = domain[len("www."):]
    return domain.split(".")[0] if domain else ""


def ga4_callback(request: HttpRequest) -> HttpResponse:
    """Odbiera kod autoryzacyjny z Google, wymienia go na `credentials` (w tym
    `refresh_token`), zapisuje token w powiązanym `Audit`, pobiera z Google Admin API
    listę wszystkich usług (properties) GA4 dostępnych dla zalogowanego konta i
    przekierowuje na stronę wyboru usługi (`select_ga4_property`) - konto Google może
    mieć dostęp do wielu usług GA4 i backend nie ma jak automatycznie ustalić, która
    z nich odpowiada audytowanej domenie."""
    audit_id = request.session.get("pending_audit_id")
    state = request.session.get("ga4_oauth_state")
    if not audit_id:
        messages.error(request, "Sesja autoryzacji Google wygasła. Spróbuj połączyć konto ponownie.")
        return redirect("auditor:index")

    audit = get_object_or_404(Audit, pk=audit_id)

    try:
        flow = Flow.from_client_secrets_file(
            str(settings.GA4_CLIENT_SECRETS_FILE),
            scopes=settings.GA4_SCOPES,
            state=state,
            redirect_uri=settings.GA4_REDIRECT_URI,
        )
        # Odtwarzamy PKCE code_verifier zapisany w start_ga4_auth - `flow` tworzony tutaj
        # to nowy obiekt (inny request niż ten, który wygenerował authorization_url), więc
        # bez tego flow.fetch_token() rzuca InvalidGrantError: "Missing code verifier".
        flow.code_verifier = request.session.get("code_verifier")
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        credentials = flow.credentials
    except Exception:
        logger.exception("Błąd podczas wymiany kodu autoryzacyjnego Google na token (audyt %s).", audit_id)
        messages.error(request, "Nie udało się połączyć z Google Analytics. Spróbuj ponownie.")
        return redirect("auditor:detail", pk=audit.pk)
    finally:
        request.session.pop("pending_audit_id", None)
        request.session.pop("ga4_oauth_state", None)
        request.session.pop("code_verifier", None)

    if credentials.refresh_token:
        audit.ga4_refresh_token = credentials.refresh_token
        audit.save(update_fields=["ga4_refresh_token"])
    else:
        logger.warning(
            "Google nie zwróciło refresh_token dla audytu %s - konto mogło już wcześniej wyrazić zgodę.", audit.pk
        )

    try:
        properties = GA4OAuthService().list_accessible_properties(credentials)
    except Exception:
        logger.exception("Nie udało się pobrać listy usług GA4 dla audytu %s.", audit.pk)
        properties = []

    if not properties:
        messages.warning(
            request,
            "Połączono z Google Analytics, ale to konto nie ma dostępu do żadnej usługi GA4.",
        )
        return redirect("auditor:detail", pk=audit.pk)

    request.session[f"ga4_properties_{audit.pk}"] = properties
    messages.success(request, "Połączono z Google. Wybierz teraz usługę Google Analytics 4.")
    return redirect("auditor:select_ga4_property", pk=audit.pk)


def select_ga4_property(request: HttpRequest, pk: int) -> HttpResponse:
    """Krok pośredni po autoryzacji Google: prezentuje listę usług (properties) GA4
    dostępnych dla zalogowanego konta (pobraną w `ga4_callback` i zapisaną w sesji) i
    pozwala użytkownikowi ręcznie wskazać, która z nich odpowiada audytowanej domenie.

    Po zatwierdzeniu formularza (POST) zapisuje wybrany `ga4_property_id`, odtwarza
    `Credentials` z zapisanego `ga4_refresh_token` i przez `AuditService.sync_ga4_data`
    pobiera oraz zapisuje statystyki ruchu organicznego z GA4.
    """
    audit = get_object_or_404(Audit, pk=pk)
    session_key = f"ga4_properties_{audit.pk}"

    if request.method == "POST":
        property_id = request.POST.get("property_id", "").strip()
        if not property_id:
            messages.error(request, "Wybierz usługę Google Analytics 4 z listy.")
            return redirect("auditor:select_ga4_property", pk=audit.pk)

        if not audit.ga4_refresh_token:
            messages.error(request, "Brak zapisanego połączenia z Google - połącz konto ponownie.")
            return redirect("auditor:detail", pk=audit.pk)

        try:
            credentials = _build_credentials_from_refresh_token(audit)
        except (FileNotFoundError, KeyError):
            logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
            messages.error(request, "Konfiguracja Google Analytics jest niekompletna. Spróbuj połączyć konto ponownie.")
            return redirect("auditor:detail", pk=audit.pk)

        AuditService().sync_ga4_data(audit, credentials, property_id)
        request.session.pop(session_key, None)
        messages.success(request, "Wybrano usługę GA4 i pobrano dane o ruchu organicznym.")
        return redirect("auditor:detail", pk=audit.pk)

    properties = request.session.get(session_key, [])
    if not properties:
        messages.error(request, "Lista usług GA4 wygasła. Połącz konto Google ponownie.")
        return redirect("auditor:detail", pk=audit.pk)

    # Zaznaczamy co najwyżej JEDNĄ opcję (pierwsze dopasowanie) - <select> z wieloma
    # atrybutami "selected" jednocześnie jest niepoprawnym/mylącym znacznikiem HTML.
    brand_token = _brand_token(audit.url)
    has_auto_selected = False
    for prop in properties:
        is_match = not has_auto_selected and bool(brand_token) and brand_token in prop["display_name"].lower()
        prop["auto_selected"] = is_match
        has_auto_selected = has_auto_selected or is_match

    return render(
        request,
        "auditor/select_property.html",
        {"audit": audit, "properties": properties, "has_auto_selected": has_auto_selected},
    )


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
# `_annotate_metric_labels`). Kilka technicznych kluczy dzieli tę samą oficjalną
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
# `_build_schema_status_table`.
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


def _build_schema_status_table(metrics):
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


def _group_technical_accordions(metrics):
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


def _split_recommendation_segments(recommendation):
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


def _annotate_metric_labels(metrics):
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
        metric.recommendation_segments = _split_recommendation_segments(recommendation)
    return metrics


# Metryki score per urządzenie zastąpione są w podsumowaniu (Priorytety/Ostrzeżenia)
# jedną zbiorczą metryką "pagespeed_score" - nie pokazujemy ich tam osobno.
MERGED_PAGESPEED_SCORE_KEYS = {"mobile_pagespeed_score", "desktop_pagespeed_score"}


def _compute_category_scores(audit):
    """Liczy % wyniku dla każdej z 4 kategorii (do pasków postępu w panelu "Przegląd"),
    tą samą metodą co ogólny wynik audytu (średnia ważona statusów ok/warning/error)."""
    results = []
    for category, label in OVERVIEW_CATEGORY_ORDER:
        category_metrics = list(audit.metrics.filter(category=category))
        if category_metrics:
            total = sum(SCORE_WEIGHTS[m.status] for m in category_metrics)
            score = round(total / len(category_metrics))
        else:
            score = 0
        results.append({"label": label, "score": score})
    return results


def _score_bucket(score):
    if score >= 80:
        return "ok"
    if score >= 50:
        return "warning"
    return "error"


def _handle_ga4_lead_event_selection(request: HttpRequest, audit: Audit) -> None:
    """Zapisuje wybrane przez użytkownika zdarzenie lead/konwersja GA4 i odświeża
    pełną analitykę (dane wielokanałowe + automatyczne wnioski SEO) - wywoływane z
    formularza POST w `audit_detail`. Pusty wybór czyści `ga4_selected_lead_event`."""
    event_name = request.POST.get("ga4_selected_lead_event", "").strip()

    if not audit.ga4_refresh_token or not audit.ga4_property_id:
        messages.error(request, "Połącz najpierw konto Google Analytics, żeby wybrać zdarzenie.")
        return

    try:
        credentials = _build_credentials_from_refresh_token(audit)
    except (FileNotFoundError, KeyError):
        logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s.", audit.pk)
        messages.error(request, "Sesja Google wygasła. Połącz konto ponownie.")
        return

    AuditService().refresh_ga4_lead_event(audit, credentials, event_name or None)
    messages.success(request, "Zaktualizowano analitykę GA4.")


def audit_detail(request, pk):
    audit = get_object_or_404(Audit, pk=pk)

    if request.method == "POST" and "ga4_selected_lead_event" in request.POST:
        _handle_ga4_lead_event_selection(request, audit)
        return redirect("auditor:detail", pk=audit.pk)

    all_metrics = _annotate_metric_labels(list(audit.metrics.all()))

    # W podsumowaniu pomijamy osobne metryki score per urządzenie - reprezentuje je
    # jedna zbiorcza metryka "pagespeed_score".
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    critical_errors = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]
    warnings = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]
    passed_tests = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]
    info_tests = [m for m in summary_metrics if m.status == AuditMetric.MetricStatus.INFO]

    stats = {
        "errors_count": len(critical_errors),
        "warnings_count": len(warnings),
        "passed_count": len(passed_tests),
        "info_count": len(info_tests),
        # "Zgodne ze standardem" = testy zdane (OK) + opcjonalne (INFO) - oba nie są
        # problemem do naprawy, tylko WARNING/ERROR wymagają uwagi.
        "compliant_count": len(passed_tests) + len(info_tests),
        "total_count": len(summary_metrics),
    }

    # Zakładka "Audyt Techniczny": 4 tematyczne akordeony (Progressive Disclosure) +
    # dedykowana tabela statusów Schema.org, budowane z tych samych summary_metrics.
    technical_accordions = _group_technical_accordions(summary_metrics)
    schema_status_table = _build_schema_status_table(summary_metrics)

    # Lista zdarzeń GA4 do formularza wyboru leadu/konwersji - pobierana na żywo tylko
    # gdy usługa jest już połączona; błąd (np. wygasły token) nie blokuje reszty strony,
    # formularz wyboru zdarzenia po prostu wtedy nie pokaże żadnych opcji.
    ga4_available_events: list[str] = []
    if audit.ga4_refresh_token and audit.ga4_property_id:
        try:
            credentials = _build_credentials_from_refresh_token(audit)
            ga4_available_events = GA4OAuthService().get_available_events(credentials, audit.ga4_property_id)
        except (FileNotFoundError, KeyError):
            logger.exception("Nie udało się odtworzyć poświadczeń Google dla audytu %s (lista zdarzeń GA4).", audit.pk)

    # Zbiorcza flaga: czy na stronie w ogóle trzeba wczytać Chart.js (Senuto i/lub GA4
    # mają jakiekolwiek dane do narysowania). Liczona tutaj, a nie jako złożony warunek
    # and/or w szablonie, żeby uniknąć pomyłek z precedencją operatorów w templatce.
    ga4_lead_insights = audit.ga4_insights.get("lead_insights") or {}
    show_charts_js = bool(
        audit.senuto_history.get("dates")
        or (audit.ga4_refresh_token and audit.ga4_history.get("dates"))
        or (audit.ga4_refresh_token and audit.ga4_channels_history.get("months"))
        or (audit.ga4_refresh_token and ga4_lead_insights.get("history", {}).get("months"))
    )

    return render(
        request,
        "auditor/detail.html",
        {
            "audit": audit,
            "critical_errors": critical_errors,
            "warnings": warnings,
            "passed_tests": passed_tests,
            "info_tests": info_tests,
            "stats": stats,
            "technical_accordions": technical_accordions,
            "schema_status_table": schema_status_table,
            "category_scores": _compute_category_scores(audit) if audit.status == "completed" else [],
            "score_bucket": _score_bucket(audit.score),
            "ga4_available_events": ga4_available_events,
            "ga4_lead_insights": ga4_lead_insights,
            "show_charts_js": show_charts_js,
        },
    )


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


def _priority_for_metric(metric):
    base = PRIORITY_BASE_BY_STATUS.get(metric.status, 3)
    boost = PRIORITY_BOOST_BY_CATEGORY.get(metric.category, 0)
    return min(10, base + boost)


def download_pdf_report(request, audit_id):
    """Generuje drukowalny (HTML -> Zapisz jako PDF w przeglądarce) raport z audytu,
    w formalnym układzie agencyjnym: okładka, spis treści, tabela priorytetów
    wdrożeniowych oraz rozdziały tematyczne."""
    audit = get_object_or_404(Audit, pk=audit_id)

    if audit.status != Audit.Status.COMPLETED:
        messages.error(request, "Raport PDF jest dostępny wyłącznie dla zakończonych audytów.")
        return redirect("auditor:detail", pk=audit.pk)

    all_metrics = _annotate_metric_labels(list(audit.metrics.all()))
    summary_metrics = [m for m in all_metrics if m.key not in MERGED_PAGESPEED_SCORE_KEYS]

    priority_findings = [
        m for m in summary_metrics
        if m.status in (AuditMetric.MetricStatus.ERROR, AuditMetric.MetricStatus.WARNING)
    ]
    for metric in priority_findings:
        metric.priority = _priority_for_metric(metric)
        metric.team = TEAM_BY_CATEGORY.get(metric.category, "IT")
    priority_findings.sort(key=lambda m: m.priority, reverse=True)

    stats = {
        "errors_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.ERROR]),
        "warnings_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.WARNING]),
        "passed_count": len([m for m in summary_metrics if m.status == AuditMetric.MetricStatus.OK]),
    }

    return render(
        request,
        "auditor/report_pdf.html",
        {
            "audit": audit,
            "generated_at": timezone.now(),
            "category_scores": _compute_category_scores(audit),
            "score_bucket": _score_bucket(audit.score),
            "stats": stats,
            "priority_findings": priority_findings,
            # Rozdział 1: Analiza Techniczna (technical + performance).
            "technical_metrics": [m for m in all_metrics if m.category in ("technical", "performance")],
            # Rozdział 2: Meta Tagi.
            "meta_tag_metrics": [m for m in all_metrics if m.category == "seo"],
            # Rozdział 3: Dane Strukturalne Schema.org.
            "schema_metrics": [m for m in all_metrics if m.key.startswith("schema_")],
            # Rozdział 4: Widoczność w AI Overviews (GEO) - pozostałe metryki structure.
            "geo_metrics": [
                m for m in all_metrics if m.category == "structure" and not m.key.startswith("schema_")
            ],
        },
    )
