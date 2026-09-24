"""Definicja trzech narzędzi aplikacji i mapowanie adresów na sekcje.

Jedno źródło prawdy dla hubu (kafelki), paska przełącznika w panelach i procesora
kontekstu. Dodanie czwartego narzędzia to jeden wpis w `TOOLS`.
"""
from __future__ import annotations

SECTION_ANALYTICS = "analytics"
SECTION_TECHNICAL = "technical"
SECTION_GEO = "geo"
SECTION_HUB = "hub"

TOOLS: tuple[dict, ...] = (
    {
        "key": SECTION_ANALYTICS,
        "icon": "📈",
        "title": "Analityka i Ruch",
        "subtitle": "GA4 / GSC",
        "description": (
            "Śledź sesje, kliknięcia, CTR oraz konwersje z Google Analytics 4 "
            "i Search Console."
        ),
        "cta": "Otwórz panel analityki",
        "url_name": "auditor:analytics",
    },
    {
        "key": SECTION_TECHNICAL,
        "icon": "⚙️",
        "title": "Audyt Techniczny",
        "subtitle": "Skaner i RAG",
        "description": (
            "Głęboki skan SSR/CSR, E-E-A-T, wykrywanie Prerender.io "
            "i automatyczne zalecenia AI."
        ),
        "cta": "Uruchom skaner SEO",
        "url_name": "auditor:index",
    },
    {
        "key": SECTION_GEO,
        "icon": "🤖",
        "title": "Widoczność w AI",
        "subtitle": "GEO Tracker",
        "description": (
            "Badanie stochastyczne (5x5) cytowalności marki w ChatGPT "
            "i Perplexity, analiza konkurencji."
        ),
        "cta": "Uruchom symulator GEO",
        "url_name": "auditor:geo_dashboard",
    },
)

# Prefiks ścieżki -> sekcja. Kolejność ma znaczenie: wygrywa pierwsze dopasowanie,
# a "/" trafia się dopiero wtedy, gdy nic wcześniej nie pasowało - czyli na hubie.
PATH_SECTIONS: tuple[tuple[str, str], ...] = (
    ("/geo-visibility/", SECTION_GEO),
    ("/analytics/", SECTION_ANALYTICS),
    ("/ga4/", SECTION_ANALYTICS),
    ("/gsc/", SECTION_ANALYTICS),
    ("/audits/", SECTION_TECHNICAL),
    ("/", SECTION_HUB),
)


def resolve_nav_section(path: str) -> str | None:
    """Która sekcja odpowiada tej ścieżce."""
    for prefix, section in PATH_SECTIONS:
        if path.startswith(prefix):
            return section
    return None


def other_tools(section: str | None) -> list[dict]:
    """Narzędzia poza bieżącym - do przełącznika w pasku panelu."""
    return [tool for tool in TOOLS if tool["key"] != section]
