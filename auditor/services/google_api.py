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


def normalize_host(value: str) -> str:
    """Dowolny zapis adresu sprowadzony do czystej nazwy hosta.

    Jedno miejsce na tę regułę, bo porównania robią tu cztery funkcje, a ich
    rozjechanie kończy się odmową dostępu do domeny, do której konto dostęp MA -
    co użytkownik czyta jako wygaśnięcie tokena, choć token jest w porządku.

    Rozumie wszystkie postacie, w jakich ta sama domena występuje w Google i w polu
    formularza: "https://klienttestowy.pl/", "http://klienttestowy.pl", "www.klienttestowy.pl",
    "klienttestowy.pl/blog", "klienttestowy.pl:8080", "sc-domain:klienttestowy.pl" oraz samo
    "klienttestowy.pl".
    """
    tekst = (value or "").strip().lower()
    if not tekst:
        return ""

    # Usługa domenowa Search Console: "sc-domain:" zdejmujemy przed resztą, bo
    # inaczej dwukropek wyglądałby jak numer portu.
    if tekst.startswith(DOMAIN_PROPERTY_PREFIX):
        tekst = tekst[len(DOMAIN_PROPERTY_PREFIX):]

    if "//" in tekst:
        tekst = urlparse(tekst).hostname or tekst.split("//", 1)[1]

    # Bez schematu `urlparse` nie rozpoznaje hosta, więc ścieżkę, zapytanie i port
    # odcinamy sami - inaczej "klienttestowy.pl/blog" nie zrównałoby się z "klienttestowy.pl".
    tekst = tekst.split("/")[0].split("?")[0].split("#")[0].split(":")[0]

    return tekst.removeprefix("www.").strip(".")


def rank_gsc_site(site_url: str, domain: str) -> tuple[int, int, str]:
    """Klucz sortowania witryn Search Console dla danej domeny; mniejszy = lepszy.

    Konto ma zwykle dla jednej domeny KILKA usług: osobno "sc-domain:domena.pl",
    osobno prefiksy "https://domena.pl/" i "https://www.domena.pl/". Każda zbiera
    dane niezależnie, więc wybór między nimi decyduje o liczbach w raporcie.
    Pobranie pierwszej z listy (tak było wcześniej) dawało wynik zależny od
    kolejności odpowiedzi Google - stąd 1 kliknięcie przy 442 sesjach organicznych
    w Analytics.

    Kolejność:
      0. usługa DOMENOWA dokładnie tej domeny - obejmuje każdy protokół i każdą
         subdomenę, więc to jedyna usługa z pełnymi danymi witryny,
      1. usługa domenowa domeny nadrzędnej (audyt subdomeny),
      2. prefiks URL po https,
      3. prefiks URL po http,
      4. cokolwiek innego, co się dopasowało.

    Przy równym stopniu wygrywa host krótszy, czyli wariant bez "www." - to już
    tylko konwencja, bo bez odpytania API nie da się stwierdzić, na którym
    wariancie ruch faktycznie się zbiera. Dlatego `pick_gsc_site` zapisuje wtedy
    ostrzeżenie w logu z listą pozostałych kandydatów.
    """
    tekst = (site_url or "").strip()
    host = normalize_host(tekst)
    cel = normalize_host(domain)

    # Prefiks obejmujący podkatalog ("https://domena.pl/en/") opisuje CZĘŚĆ witryny,
    # więc przegrywa z prefiksem korzenia nawet przy tym samym protokole.
    podkatalog = 0
    if "//" in tekst:
        sciezka = urlparse(tekst if "://" in tekst else f"https://{tekst}").path
        podkatalog = 1 if sciezka.strip("/") else 0

    if tekst.lower().startswith(DOMAIN_PROPERTY_PREFIX):
        stopien = 0 if host == cel else 1
    elif tekst.lower().startswith("https://"):
        stopien = 2 + podkatalog
    elif tekst.lower().startswith("http://"):
        stopien = 4 + podkatalog
    else:
        stopien = 6

    return (stopien, len(host), tekst.lower())


def pick_gsc_site(sites: list[dict], domain: str) -> str:
    """Najlepsza witryna Search Console dla tej domeny - albo pusty ciąg.

    `sites` to wpisy z `list_gsc_sites` (klucz "site_url"). Pusty wynik znaczy
    "nie widzimy takiej witryny" i obejmuje też awarię API u wywołującego.
    """
    kandydaci = sorted(
        (
            str(w.get("site_url", ""))
            for w in sites
            if same_site(str(w.get("site_url", "")), domain)
        ),
        key=lambda u: rank_gsc_site(u, domain),
    )
    if not kandydaci:
        return ""

    wybrana = kandydaci[0]
    # Prefiksy URL bez usługi domenowej to przypadek, w którym sama postać adresu
    # NIE wystarcza: pomiar na motivationdirect.pl pokazał 1 kliknięcie na
    # "https://domena.pl/" i 1248 na "https://www.domena.pl/". Rozstrzyga wtedy
    # `pick_gsc_site_by_traffic`, a ta funkcja zostaje wyborem awaryjnym.
    if len(kandydaci) > 1 and rank_gsc_site(wybrana, domain)[0] >= 2:
        logger.warning(
            "Search Console: dla %s konto ma kilka usług prefiksowych i żadnej domenowej. "
            "Bez pomiaru ruchu wybór jest zgadywaniem - wybrano %s, pozostałe: %s.",
            domain, wybrana, ", ".join(kandydaci[1:]),
        )
    return wybrana


def matching_gsc_sites(sites: list[dict], domain: str) -> list[str]:
    """Adresy witryn dopasowanych do domeny, od najlepiej opisującej witrynę."""
    return sorted(
        (
            str(w.get("site_url", ""))
            for w in sites
            if same_site(str(w.get("site_url", "")), domain)
        ),
        key=lambda u: rank_gsc_site(u, domain),
    )


def pick_gsc_site_by_traffic(
    credentials, sites: list[dict], domain: str, days: int = 92
) -> str:
    """Wybór usługi Search Console na podstawie tego, GDZIE SĄ DANE.

    Postać adresu nie wystarcza, gdy konto nie ma usługi domenowej: dla
    motivationdirect.pl prefiks "https://domena.pl/" miał 1 kliknięcie, a
    "https://www.domena.pl/" - 1248. Który wariant zbiera ruch, zależy od tego,
    na który host witryna przekierowuje, a tego z samej nazwy nie widać.

    Odpytujemy więc kandydatów o sumę wyświetleń w oknie `days` i bierzemy
    największą. Wyświetlenia, a nie kliknięcia: witryna o małym ruchu może mieć
    zero kliknięć na każdym wariancie, a wyświetlenia i tak wskażą właściwy.

    Pomiar robimy TYLKO wtedy, gdy jest co rozstrzygać - przy usłudze domenowej
    albo jednym kandydacie wynik bierze się z samej postaci adresu. Przy
    jakimkolwiek błędzie wracamy do `pick_gsc_site`, bo brak danych nie może
    zablokować raportu.
    """
    kandydaci = matching_gsc_sites(sites, domain)
    if not kandydaci:
        return ""

    najlepsza_ranga = rank_gsc_site(kandydaci[0], domain)[0]
    if najlepsza_ranga < 2:
        return kandydaci[0]

    rownorzedni = [k for k in kandydaci if rank_gsc_site(k, domain)[0] == najlepsza_ranga]
    if len(rownorzedni) == 1:
        return rownorzedni[0]

    from datetime import date, timedelta

    from django.conf import settings
    from django.core.cache import cache

    # Klucz z listy KANDYDATÓW, nie z domeny: gdy konto traci lub zyskuje usługę,
    # pomiar trzeba powtórzyć. Zapamiętujemy sam wynik porównania, więc późniejsza
    # awaria listowania witryn nie udaje, że witryna jest widoczna.
    klucz = "gsc_traffic_choice:" + "|".join(rownorzedni)
    zapamietany = cache.get(klucz)
    if zapamietany is not None:
        return zapamietany

    # Ostatnie dni Google jeszcze uzupełnia, więc okno kończymy z zapasem.
    koniec = date.today() - timedelta(days=3)
    start = koniec - timedelta(days=days)
    body = {
        "startDate": start.isoformat(),
        "endDate": koniec.isoformat(),
        "dimensions": [],
        "type": "web",
        "dataState": "final",
    }

    try:
        from googleapiclient.discovery import build

        service = build("searchconsole", "v1", credentials=credentials, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udało się zbudować klienta Search Console: %s", exc)
        return pick_gsc_site(sites, domain)

    pomiary: list[tuple[int, str]] = []
    for adres in rownorzedni:
        try:
            odpowiedz = service.searchanalytics().query(siteUrl=adres, body=body).execute()
            wiersze = (odpowiedz or {}).get("rows") or []
            wyswietlenia = round(wiersze[0].get("impressions", 0)) if wiersze else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("Search Console: nie udało się odpytać %s: %s", adres, exc)
            continue
        pomiary.append((wyswietlenia, adres))

    if not pomiary:
        return pick_gsc_site(sites, domain)

    pomiary.sort(key=lambda p: (-p[0], rank_gsc_site(p[1], domain)))
    wybrana = pomiary[0][1]
    logger.info(
        "Search Console: dla %s wybrano %s na podstawie wyświetleń (%s).",
        domain,
        wybrana,
        ", ".join(f"{adres}: {ile}" for ile, adres in pomiary),
    )
    cache.set(klucz, wybrana, getattr(settings, "CACHE_TTL_GSC", 12 * 3600))
    return wybrana


def same_site(first: str, second: str) -> bool:
    """Czy dwa adresy opisują tę samą witrynę, z tolerancją na subdomeny.

    Tolerancja jest potrzebna w obie strony: usługa "sklep.amso.eu" obsługuje audyt
    "amso.eu", a audyt subdomeny bywa opisywany usługą domeny głównej. Puste wejście
    nie jest dopasowaniem - "nie wiem" to nie "to samo".
    """
    pierwszy, drugi = normalize_host(first), normalize_host(second)
    if not (pierwszy and drugi):
        return False
    return (
        pierwszy == drugi
        or pierwszy.endswith("." + drugi)
        or drugi.endswith("." + pierwszy)
    )


def gsc_site_domain(site_url: str) -> str:
    """Domena usługi Search Console - wspólna postać dla obu typów usług."""
    return normalize_host(site_url)


def audit_domain(audit_url: str) -> str:
    """Domena audytu w postaci porównywalnej z domenami usług Google."""
    return normalize_host(audit_url)


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
    # Przez normalizator, bo w nazwie usługi bywa pełny adres ze schematem i ukośnikiem,
    # a porównanie z listą domen odbywa się na czystym hoście.
    return normalize_host(dopasowanie.group(1)) if dopasowanie else ""


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

    Oba porównywane adresy przechodzą przez `normalize_host`. Wcześniej szukana
    wartość traciła tylko końcowy ukośnik, więc wpisanie "https://przyklad.pl/"
    nigdy nie trafiało w "przyklad.pl" - schemat zostawał w ciągu.
    """
    szukana = normalize_host(wanted)
    if not szukana:
        return None

    for wpis in domains:
        domena = normalize_host(wpis.get("domain", ""))
        if not domena:
            continue
        if domena == szukana or szukana.endswith("." + domena) or domena.endswith("." + szukana):
            return wpis
    return None
