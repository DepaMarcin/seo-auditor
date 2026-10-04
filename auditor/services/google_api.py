"""Wspólne odpytywanie API Google na potrzeby panelu analityki.

Panel musi pokazać trzy rzeczy, których nie da się odczytać z bazy: czyje konto Google
jest podłączone, jakie usługi GA4 są dla niego dostępne i jakie witryny są zweryfikowane
w Search Console. Każde z nich to inne API, ale wszystkie korzystają z tego samego
tokenu odświeżania, dlatego trzymamy je razem.

Żadna z tych funkcji nie podnosi wyjątku: panel analityki ma się wyrenderować także
wtedy, gdy Google chwilowo nie odpowiada albo token stracił ważność - użytkownik
zobaczy wtedy prośbę o ponowne połączenie zamiast błędu 500.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Przedrostek usługi domenowej w Search Console: "sc-domain:przyklad.pl" obejmuje
# wszystkie subdomeny i protokoły, w odróżnieniu od usługi z prefiksem URL.
DOMAIN_PROPERTY_PREFIX = "sc-domain:"


def fetch_account_email(credentials) -> str:
    """Adres konta Google, do którego należy token.

    Wymaga zakresu `userinfo.email`. Tokeny wydane przed jego dodaniem go nie mają -
    zwracamy wtedy pusty string, a panel poprosi o ponowne połączenie konta.
    """
    try:
        from googleapiclient.discovery import build

        service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
        return (service.userinfo().get().execute() or {}).get("email", "")
    except Exception as exc:  # noqa: BLE001 - brak adresu nie może wywrócić panelu
        logger.info("Nie udało się odczytać adresu konta Google: %s", exc)
        return ""


def list_gsc_sites(credentials) -> list[dict]:
    """Witryny zweryfikowane w Search Console.

    Zwraca wyłącznie te, do których konto ma prawo odczytu danych - usługi o poziomie
    dostępu "siteUnverifiedUser" widać na liście, ale zapytanie o ich dane kończy się
    błędem, więc pokazywanie ich w selektorze byłoby mylące.
    """
    try:
        from googleapiclient.discovery import build

        service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
        response = service.sites().list().execute() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać listy witryn Search Console: %s", exc)
        return []

    witryny = []
    for wpis in response.get("siteEntry", []):
        site_url = wpis.get("siteUrl", "")
        if not site_url or wpis.get("permissionLevel") == "siteUnverifiedUser":
            continue
        witryny.append({
            "site_url": site_url,
            "label": _gsc_label(site_url),
            "domain": gsc_site_domain(site_url),
        })

    return sorted(witryny, key=lambda w: w["label"])


def _gsc_label(site_url: str) -> str:
    """Etykieta w selektorze: usługę domenową opisujemy wprost, bo wygląda inaczej."""
    if site_url.startswith(DOMAIN_PROPERTY_PREFIX):
        return f"{site_url[len(DOMAIN_PROPERTY_PREFIX):]} (usługa domenowa)"
    return site_url


def gsc_site_domain(site_url: str) -> str:
    """Domena usługi Search Console - wspólna postać dla obu typów usług."""
    if site_url.startswith(DOMAIN_PROPERTY_PREFIX):
        domena = site_url[len(DOMAIN_PROPERTY_PREFIX):]
    else:
        domena = urlparse(site_url).hostname or site_url
    return domena.lower().removeprefix("www.").rstrip("/")


def audit_domain(audit_url: str) -> str:
    """Domena audytu w postaci porównywalnej z domenami usług Google."""
    hostname = urlparse(audit_url).hostname or audit_url
    return hostname.lower().removeprefix("www.").rstrip("/")


def mark_suggestions(
    options: list[dict], domain: str, key: str, mode: str = "domain"
) -> list[dict]:
    """Oznacza opcje pasujące do domeny audytu i przenosi je na początek listy.

    Przy kilkunastu usługach na koncie trafienie we właściwą jest kwestią uwagi -
    podpowiedź zdejmuje z użytkownika szukanie, nie odbierając mu wyboru.

    `mode="domain"` porównuje domeny (Search Console zwraca prawdziwe adresy).
    `mode="text"` szuka domeny wewnątrz tekstu - nazwa usługi GA4 to dowolny opis
    nadany przez właściciela, np. "przyklad.pl - GA4" albo "Sklep (przyklad.pl)".
    """
    if not domain:
        return options

    for opcja in options:
        kandydat = (opcja.get(key) or "").lower()
        if not kandydat:
            opcja["suggested"] = False
        elif mode == "text":
            opcja["suggested"] = domain in kandydat
        else:
            # Dopasowanie obejmuje subdomeny: usługa "sklep.przyklad.pl" pasuje do
            # audytu "przyklad.pl" i odwrotnie.
            opcja["suggested"] = (
                kandydat == domain
                or kandydat.endswith("." + domain)
                or domain.endswith("." + kandydat)
            )

    return sorted(options, key=lambda o: (not o.get("suggested"), o.get("label", "")))


def list_ga4_properties(credentials) -> list[dict]:
    """Usługi GA4, do których konto ma dostęp - z nazwą konta nadrzędnego."""
    from auditor.services.ga4_service import GA4OAuthService

    try:
        return GA4OAuthService().list_accessible_properties(credentials)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się pobrać listy usług GA4: %s", exc)
        return []


def property_domain(display_name: str) -> str:
    """Domena wyłuskana z nazwy usługi GA4.

    Nazwa usługi to dowolny tekst nadany przez właściciela ("przyklad.pl - GA4",
    "Sklep (przyklad.pl)"), więc szukamy w niej czegoś, co wygląda jak domena.
    """
    import re

    dopasowanie = re.search(r"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)\b", (display_name or "").lower())
    return dopasowanie.group(1).removeprefix("www.") if dopasowanie else ""


def list_authorized_domains(credentials) -> list[dict]:
    """Domeny, do których zalogowane konto Google MA dostęp.

    Źródłem prawdy jest Google, nie nasza baza: wpis w bazie mówi tylko, że ktoś
    kiedyś coś przypisał, a nie że bieżące konto ma do tego uprawnienia. Łączymy
    dwie listy, bo dostęp do GA4 i do Search Console nadaje się niezależnie -
    bywa jeden bez drugiego.
    """
    domeny: dict[str, dict] = {}

    for wlasciwosc in list_ga4_properties(credentials):
        domena = property_domain(wlasciwosc.get("display_name", ""))
        if not domena:
            continue
        wpis = domeny.setdefault(domena, {"domain": domena, "ga4": [], "gsc": []})
        wpis["ga4"].append({
            "property_id": str(wlasciwosc.get("property_id", "")),
            "display_name": wlasciwosc.get("display_name", ""),
            "account_name": wlasciwosc.get("account_name", ""),
        })

    for witryna in list_gsc_sites(credentials):
        domena = witryna["domain"]
        if not domena:
            continue
        wpis = domeny.setdefault(domena, {"domain": domena, "ga4": [], "gsc": []})
        wpis["gsc"].append(witryna)

    return sorted(domeny.values(), key=lambda w: w["domain"])


def find_authorized_domain(domains: list[dict], wanted: str) -> dict | None:
    """Wpis z listy uprawnień odpowiadający szukanej domenie.

    Dopasowanie obejmuje subdomeny w obie strony: usługa "sklep.przyklad.pl" obsługuje
    audyt "przyklad.pl" i odwrotnie - w Google jedno konto zwykle ma dostęp do całej
    rodziny adresów jednej firmy.
    """
    szukana = (wanted or "").lower().removeprefix("www.").strip().rstrip("/")
    if not szukana:
        return None

    for wpis in domains:
        domena = wpis["domain"]
        if domena == szukana or szukana.endswith("." + domena) or domena.endswith("." + szukana):
            return wpis
    return None
