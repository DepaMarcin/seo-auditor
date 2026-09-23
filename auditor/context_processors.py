"""Procesory kontekstu szablonów."""
from __future__ import annotations

from django.http import HttpRequest

# Widoki, na których nawigacja wewnętrzna i panel użytkownika nie mają się pojawiać.
# Ekran logowania ma być czysty także dla użytkownika, który JUŻ jest zalogowany -
# wchodzi tam zwykle po to, by zalogować się na inne konto, a pasek sekcji i przycisk
# "Wyloguj" tylko rozpraszają uwagę od formularza.
CHROMELESS_VIEWS = frozenset({"login", "logout", "password_reset", "password_reset_confirm"})

SECTION_ANALYTICS = "analytics"
SECTION_TECHNICAL = "technical"
SECTION_GEO = "geo"

# Prefiks ścieżki -> podświetlana zakładka. Kolejność ma znaczenie: wygrywa pierwsze
# dopasowanie, a "/" na końcu działa jak domyślna sekcja. Prefiksy "/gsc/" i
# "/analytics/" nie mają jeszcze własnych tras - są tu na wyrost, żeby pasek zadziałał
# od razu, gdy analityka dostanie osobne widoki.
PATH_SECTIONS: tuple[tuple[str, str], ...] = (
    ("/geo-visibility/", SECTION_GEO),
    ("/ga4/", SECTION_ANALYTICS),
    ("/gsc/", SECTION_ANALYTICS),
    ("/analytics/", SECTION_ANALYTICS),
    ("/audits/", SECTION_TECHNICAL),
    ("/", SECTION_TECHNICAL),
)


def resolve_nav_section(path: str) -> str | None:
    """Która zakładka odpowiada tej ścieżce."""
    for prefix, section in PATH_SECTIONS:
        if path.startswith(prefix):
            return section
    return None


def navigation(request: HttpRequest) -> dict:
    """Czy pokazać pasek sekcji i panel użytkownika oraz którą zakładkę podświetlić.

    Jeden warunek zamiast powtarzania go w kilku szablonach - dzięki temu nagłówek
    i nawigacja nie mogą się rozjechać (np. ukryty pasek przy widocznym "Wyloguj").
    """
    user = getattr(request, "user", None)
    authenticated = bool(user and user.is_authenticated)

    # `resolver_match` bywa None, zanim Django dopasuje trasę - m.in. przy stronach
    # błędów 404/500 renderowanych poza normalnym przepływem widoku.
    match = getattr(request, "resolver_match", None)
    view_name = getattr(match, "url_name", None) if match else None

    visible = authenticated and view_name not in CHROMELESS_VIEWS

    # Widok może nadpisać wyliczoną sekcję własnym `nav_section` w kontekście -
    # słownik widoku leży w `RequestContext` nad procesorami.
    return {
        "nav_visible": visible,
        "user_panel_visible": visible,
        "nav_section": resolve_nav_section(request.path),
    }
