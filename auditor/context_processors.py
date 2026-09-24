"""Procesory kontekstu szablonów."""
from __future__ import annotations

from django.http import HttpRequest

from auditor.navigation import SECTION_HUB, other_tools, resolve_nav_section

__all__ = ["navigation", "resolve_nav_section", "CHROMELESS_VIEWS"]

# Widoki, na których pasek narzędzi i panel użytkownika nie mają się pojawiać.
# Ekran logowania ma być czysty także dla użytkownika, który JUŻ jest zalogowany -
# wchodzi tam zwykle po to, by zalogować się na inne konto, a pasek i przycisk
# "Wyloguj" tylko rozpraszają uwagę od formularza.
CHROMELESS_VIEWS = frozenset({"login", "logout", "password_reset", "password_reset_confirm"})


def navigation(request: HttpRequest) -> dict:
    """Pasek powrotu, przełącznik narzędzi i panel użytkownika.

    Jeden warunek zamiast powtarzania go w kilku szablonach - dzięki temu nagłówek
    i pasek nie mogą się rozjechać (np. ukryty pasek przy widocznym "Wyloguj").
    """
    user = getattr(request, "user", None)
    authenticated = bool(user and user.is_authenticated)

    # `resolver_match` bywa None, zanim Django dopasuje trasę - m.in. przy stronach
    # błędów 404/500 renderowanych poza normalnym przepływem widoku.
    match = getattr(request, "resolver_match", None)
    view_name = getattr(match, "url_name", None) if match else None
    on_chrome_page = authenticated and view_name not in CHROMELESS_VIEWS

    section = resolve_nav_section(request.path)

    # Na hubie pasek powrotu byłby linkiem do strony, na której już jesteśmy -
    # kafelki są tam całą nawigacją.
    return {
        "nav_visible": on_chrome_page and section != SECTION_HUB,
        "user_panel_visible": on_chrome_page,
        "nav_section": section,
        "nav_other_tools": other_tools(section),
    }
