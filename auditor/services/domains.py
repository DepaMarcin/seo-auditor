"""Domeny, którymi użytkownik już się zajmował - wspólne dla wszystkich modułów.

Hub potrzebuje jednej listy niezależnej od tego, w którym narzędziu domena się
pojawiła. Audyt techniczny, podłączona analityka i badanie GEO to trzy osobne tabele,
ale dla użytkownika to jedna witryna - i to ona, nie rekord, jest tu jednostką.
"""
from __future__ import annotations

RECENT_DOMAINS_LIMIT = 10

# Ile najnowszych rekordów z każdej tabeli bierzemy pod uwagę. Domen szukamy w Pythonie
# (adres audytu trzeba najpierw sprowadzić do domeny), więc bez tego limitu konto
# z tysiącem skanów ładowałoby je wszystkie przy każdym wejściu na hub.
SCAN_LIMIT = 200

SOURCE_LABEL_AUDIT = "audyt"
SOURCE_LABEL_ANALYTICS = "analityka"
SOURCE_LABEL_GEO = "GEO"

# Stała kolejność etykiet - inaczej ta sama domena opisywałaby się raz "GEO, audyt",
# raz "audyt, GEO", zależnie od tego, które badanie było świeższe.
_LABEL_ORDER = (SOURCE_LABEL_AUDIT, SOURCE_LABEL_ANALYTICS, SOURCE_LABEL_GEO)


def recent_domains_for(owner, limit: int = RECENT_DOMAINS_LIMIT) -> list[dict]:
    """Ostatnie domeny użytkownika jako `[{"domain", "sources"}]`, od najnowszej.

    Kolejność bierze się z czasu ostatniej aktywności, a nie z nazwy: na górze ma być
    to, czym użytkownik zajmował się ostatnio. Etykiety źródeł mówią, czy synteza
    będzie miała z czego powstać - domena bez danych dałaby raport o samym ich braku.
    """
    from auditor.models import Audit, GeoStudy
    from auditor.services.geo import normalize_domain
    from auditor.services.google_api import audit_domain

    if owner is None or not getattr(owner, "is_authenticated", False):
        return []

    # Jedno przejście po audytach: rekordy analityczne i skany techniczne leżą w tej
    # samej tabeli, a różni je tylko flaga.
    zdarzenia: list[tuple] = [
        (
            audyt.created_at,
            audit_domain(audyt.url),
            SOURCE_LABEL_ANALYTICS if audyt.analytics_only else SOURCE_LABEL_AUDIT,
        )
        for audyt in Audit.objects.filter(owner=owner)
        .only("url", "analytics_only", "created_at")
        .order_by("-created_at")[:SCAN_LIMIT]
    ]
    zdarzenia += [
        (badanie.created_at, normalize_domain(badanie.domain), SOURCE_LABEL_GEO)
        for badanie in GeoStudy.objects.filter(owner=owner)
        .only("domain", "created_at")
        .order_by("-created_at")[:SCAN_LIMIT]
    ]

    zdarzenia.sort(key=lambda zdarzenie: zdarzenie[0], reverse=True)

    kolejnosc: list[str] = []
    etykiety: dict[str, set] = {}
    for _, domena, etykieta in zdarzenia:
        if not domena:
            continue
        if domena not in etykiety:
            kolejnosc.append(domena)
            etykiety[domena] = set()
        etykiety[domena].add(etykieta)

    return [
        {
            "domain": domena,
            "sources": [label for label in _LABEL_ORDER if label in etykiety[domena]],
        }
        for domena in kolejnosc[:limit]
    ]
