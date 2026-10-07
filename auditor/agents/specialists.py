"""Wyspecjalizowani agenci badania.

Każdy agent dostaje wspólny stan, dokłada do niego swoje wnioski i nie podnosi
wyjątków na zewnątrz - awarię zapisuje w `state.errors`. Dwóch pierwszych nie używa
modelu językowego: ich zadaniem jest przełożyć liczby na zdania, a to reguły, nie
interpretacja. LLM wchodzi dopiero przy syntezie, gdzie faktycznie wnosi wartość.
"""
from __future__ import annotations

import logging

from .state import SEOInvestigatorState
from .tools import get_geo_visibility, get_technical_health, get_traffic_trends

logger = logging.getLogger(__name__)

# Nazwy źródeł w `state.sources` - interfejs rysuje po nich odznaki.
SOURCE_TECHNICAL = "technical"
SOURCE_ANALYTICS = "analytics"
SOURCE_GEO = "geo"

# Model syntezy. Tańszy wariant wystarcza: materiał jest już przygotowany, zadaniem
# modelu jest ułożyć go w raport, a nie szukać wniosków od zera.
REPORTER_MODEL = "gpt-4o-mini"

# Ile problemów technicznych trafia do wniosków. Lista wszystkiego byłaby zrzutem
# metryk, a nie wnioskiem - raport ma wskazywać, czym zająć się najpierw.
MAX_TECHNICAL_ISSUES = 12

# Progi istotności zmiany ruchu. Wahania poniżej tej granicy to szum sezonowy.
SIGNIFICANT_CHANGE_PERCENT = 10

REPORTER_PROMPT = """Jesteś konsultantem SEO przygotowującym podsumowanie dla klienta.

Domena: {domain}

USTALENIA TECHNICZNE:
{technical}

USTALENIA ANALITYCZNE (GA4 / Search Console):
{analytics}

WIDOCZNOŚĆ W WYSZUKIWARKACH AI:
{geo}

OGRANICZENIA BADANIA:
{errors}

ZADANIE:
Napisz zwięzły raport w języku polskim, w formacie Markdown, o strukturze:

## Główne Wnioski (Executive Summary)
Ta sekcja jest OBOWIĄZKOWA i zawsze pierwsza. Napisz 1-2 mocne akapity, w których
ŁĄCZYSZ dane z różnych sekcji ustaleń w jedną diagnozę - nie streszczaj ich po kolei.
Szukaj korelacji i pisz o nich wprost:
- Jeśli widzisz spadek sesji albo kliknięć, podaj jego skalę i WYMIEŃ z nazwy frazy,
  które straciły najwięcej, a następnie wskaż prawdopodobne przyczyny techniczne
  z ustaleń technicznych (na przykład: "Spadki na frazy X i Y mogą wynikać ze słabego
  LCP i braku nagłówka H1 na stronach ofertowych").
- Jeśli widzisz wzrosty, napisz, co zadziałało, i na których frazach to widać.
- Jeśli danych z któregoś źródła nie ma, powiedz, czego w związku z tym nie wiadomo -
  zamiast milczeć o luce.
Przyczyny podawaj jako hipotezy ("może wynikać z", "wskazuje na"), nigdy jako pewniki:
korelacja w tych danych nie dowodzi związku przyczynowego.

## Co wymaga uwagi
Lista priorytetów - od najpilniejszego. Przy każdym jedno zdanie uzasadnienia.

## Co działa dobrze
Krótka lista. Pomiń, jeśli nie ma czego pochwalić.

## Czego nie udało się zbadać
Wypisz ograniczenia z sekcji OGRANICZENIA. Pomiń całą sekcję, gdy ich nie ma.

ZASADY:
- Opieraj się WYŁĄCZNIE na przekazanych ustaleniach. Nie dopowiadaj faktów.
- Nie powtarzaj surowych nazw metryk - tłumacz je na skutek dla firmy.
- Bez wstępów o tym, czym jest SEO. Klient to wie."""


class TechnicalAgent:
    """Przekłada metryki techniczne na twarde wnioski."""

    name = "TechnicalAgent"

    def run(self, state: SEOInvestigatorState, owner=None) -> SEOInvestigatorState:
        state.mark_source(SOURCE_TECHNICAL, False)

        try:
            zdrowie = get_technical_health(state.domain, owner=owner)
        except Exception as exc:  # noqa: BLE001 - agent nie przerywa badania
            logger.exception("%s padł dla %s.", self.name, state.domain)
            state.record_error(self.name, exc)
            return state

        if zdrowie.get("error"):
            state.record_error(self.name, zdrowie["error"])
            return state

        state.mark_source(SOURCE_TECHNICAL, True)

        if zdrowie.get("audit_id"):
            state.audit_id = zdrowie["audit_id"]
        if zdrowie.get("score") is not None:
            state.metrics["technical_score"] = zdrowie["score"]
        if zdrowie.get("as_of"):
            state.data_timestamps[SOURCE_TECHNICAL] = zdrowie["as_of"]

        problemy = zdrowie.get("problems") or []
        if not problemy:
            zrodlo = "audytu" if zdrowie["source"] == "audit" else "skanu strony"
            state.technical_issues.append(
                f"Nie znaleziono błędów technicznych wymagających uwagi (na podstawie {zrodlo})."
            )
            return state

        # Liczniki bierzemy ze statusów metryk, a nie z tekstów wniosków: zdania są
        # dla czytelnika, a karta KPI potrzebuje liczby.
        state.metrics["technical_errors"] = sum(
            1 for problem in problemy if problem["status"] == "error"
        )
        state.metrics["technical_warnings"] = sum(
            1 for problem in problemy if problem["status"] == "warning"
        )

        # Błędy przed ostrzeżeniami: raport ma zaczynać się od tego, co boli najbardziej.
        kolejnosc = {"error": 0, "warning": 1}
        problemy.sort(key=lambda p: kolejnosc.get(p["status"], 2))

        if zdrowie.get("score") is not None:
            state.technical_issues.append(
                f"Wynik audytu technicznego: {zdrowie['score']}/100."
            )

        for problem in problemy[:MAX_TECHNICAL_ISSUES]:
            waga = "Błąd" if problem["status"] == "error" else "Ostrzeżenie"
            state.technical_issues.append(f"{waga}: {problem['value']}")

        pozostale = len(problemy) - MAX_TECHNICAL_ISSUES
        if pozostale > 0:
            state.technical_issues.append(
                f"Pozostało {pozostale} dalszych uwag o niższym priorytecie."
            )

        return state


class AnalyticsAgent:
    """Przekłada dane GA4 i Search Console na wnioski o ruchu."""

    name = "AnalyticsAgent"

    def run(self, state: SEOInvestigatorState, owner=None) -> SEOInvestigatorState:
        state.mark_source(SOURCE_ANALYTICS, False)

        try:
            trendy = get_traffic_trends(state.domain, owner=owner)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s padł dla %s.", self.name, state.domain)
            state.record_error(self.name, exc)
            return state

        ma_dane = bool(trendy.get("ga4") or trendy.get("gsc"))
        if not trendy.get("authorized") and not ma_dane:
            # Brak autoryzacji to normalny stan, nie awaria - zapisujemy go jako
            # ograniczenie badania, żeby raport nie udawał kompletnego. Warunek
            # obejmuje też dane: zapisane liczby przychodzą bez żywego tokena,
            # więc sam brak autoryzacji nie znaczy, że nie ma czego opisać.
            powod = trendy.get("error") or "Brak autoryzacji GA4/GSC."
            state.analytics_insights.append(f"Brak autoryzacji GA4/GSC - {powod}")
            state.record_error(self.name, powod)
            return state

        # Odznaka źródła zapala się TYLKO za twarde liczby. Zdanie o nieprzypisanej
        # usłudze też jest wnioskiem wartym raportu, ale danymi nie jest - liczenie
        # wpisów w liście zapalało ją na sam komunikat o braku danych.
        z_bazy = trendy.get("source") == "db"
        twarde_ga4 = self._describe_ga4(state, trendy.get("ga4"), z_bazy=z_bazy)
        twarde_gsc = self._describe_gsc(state, trendy.get("gsc"))
        state.mark_source(SOURCE_ANALYTICS, twarde_ga4 or twarde_gsc)

        if twarde_ga4 or twarde_gsc:
            self._record_age(state, trendy)

        if not state.analytics_insights:
            state.analytics_insights.append(
                "Konto Google jest podłączone, ale nie zwróciło danych dla tej domeny."
            )

        return state

    def _record_age(self, state: SEOInvestigatorState, trendy: dict) -> None:
        """Zapisuje datę najstarszej liczby, jaka weszła do wniosków.

        Karta jest tak świeża jak jej najstarsza składowa: GA4 i Search Console mogą
        pochodzić z różnych rekordów, a ostrzeżenie ma reagować na gorszy przypadek.
        """
        daty = [
            (zrodlo or {}).get("as_of")
            for zrodlo in (trendy.get("ga4"), trendy.get("gsc"))
        ]
        istniejace = [data for data in daty if data]
        if istniejace:
            state.data_timestamps[SOURCE_ANALYTICS] = min(istniejace, key=_as_day)

    def _describe_ga4(
        self, state: SEOInvestigatorState, ga4: dict | None, z_bazy: bool = False
    ) -> bool:
        """Dopisuje wnioski o sesjach. Zwraca True tylko dla twardych liczb."""
        if ga4 is None:
            state.analytics_insights.append(
                "Do tej domeny nie przypisano usługi Analytics 4 - brak danych o sesjach."
            )
            return False
        if ga4.get("error"):
            state.record_error(self.name, f"GA4: {ga4['error']}")
            return False

        okno = ga4.get("window_days")
        zakres = f"w ostatnich {okno} dniach" if okno else "w zapisanym okresie"
        # Przy danych z bazy mówimy to wprost: liczby mogą być starsze niż dzisiejsze,
        # a raport bez tej adnotacji przedstawiałby je jako stan na teraz.
        przypis = " (dane zapisane w bazie)" if z_bazy else ""
        state.analytics_insights.append(
            f"Sesje organiczne {zakres}: {ga4['sessions']}{przypis}."
        )
        state.metrics["organic_sessions"] = ga4["sessions"]
        state.metrics["analytics_from_database"] = z_bazy

        # Gotową zmianę procentową bierzemy od serwisu; przeliczamy sami tylko wtedy,
        # gdy jej nie podał - dwa wyniki tej samej rzeczy musiałyby się rozjechać.
        zmiana = ga4.get("organic_change_percent")
        pary = ""
        if zmiana is None:
            kanaly = ga4.get("channels") or {}
            biezace = (kanaly.get("current") or {}).get("Organic Search")
            poprzednie = (kanaly.get("previous") or {}).get("Organic Search")
            zmiana = _percent_change(biezace, poprzednie)
            if zmiana is not None:
                pary = f" ({poprzednie} → {biezace} sesji)"
        else:
            zmiana = round(zmiana)

        if zmiana is not None:
            kierunek = "wzrost" if zmiana > 0 else "spadek"
            waga = "istotny" if abs(zmiana) >= SIGNIFICANT_CHANGE_PERCENT else "nieznaczny"
            state.analytics_insights.append(
                f"Ruch organiczny rok do roku: {waga} {kierunek} o {abs(zmiana)}%{pary}."
            )
            state.metrics["organic_change_percent"] = zmiana

        return True

    def _describe_gsc(self, state: SEOInvestigatorState, gsc: dict | None) -> bool:
        """Dopisuje wnioski o kliknięciach. Zwraca True tylko dla twardych liczb."""
        if gsc is None:
            return False
        if gsc.get("error"):
            state.record_error(self.name, f"Search Console: {gsc['error']}")
            return False

        biezace = gsc.get("clicks_current")
        poprzednie = gsc.get("clicks_previous")
        if biezace:
            state.analytics_insights.append(
                f"Kliknięcia z wyszukiwarki: {biezace} (rok temu: {poprzednie})."
            )
            state.metrics["search_clicks"] = biezace

        # Serwis sam liczy zmianę rok do roku - korzystamy z jego wyniku, zamiast
        # przeliczać drugi raz i ryzykować rozbieżność.
        zmiana = gsc.get("yoy_change_percent")
        if zmiana is not None and abs(zmiana) >= SIGNIFICANT_CHANGE_PERCENT:
            kierunek = "wzrost" if zmiana > 0 else "spadek"
            state.analytics_insights.append(
                f"Kliknięcia rok do roku: {kierunek} o {abs(zmiana)}%."
            )

        rosnace = _query_names(gsc.get("gainers"))
        if rosnace:
            state.metrics["search_gainers"] = rosnace
        spadajace = _query_names(gsc.get("losers"))
        if spadajace:
            state.metrics["search_losers"] = spadajace

        # Z wielkością zmiany, nie tylko z nazwą frazy: bez liczby model nie odróżni
        # frazy, która straciła 3000 kliknięć, od tej, która straciła trzy - a od tego
        # zależy, czy warto wiązać ją z usterką techniczną.
        if rosnace:
            state.analytics_insights.append(
                f"Frazy rosnące: {_describe_queries(gsc.get('gainers'))}."
            )
        if spadajace:
            state.analytics_insights.append(
                f"Frazy spadające: {_describe_queries(gsc.get('losers'))}."
            )

        # Same frazy bez liczby kliknięć to jeszcze nie pomiar ruchu.
        return bool(biezace)


class GeoAgent:
    """Dokłada wynik ostatniego badania widoczności w wyszukiwarkach AI.

    Nie zleca nowego badania - jedno kosztuje kilkadziesiąt wywołań modelu i trwa
    minuty, więc byłoby to zaskoczeniem dla kogoś, kto prosił o raport.
    """

    name = "GeoAgent"

    def run(self, state: SEOInvestigatorState, owner=None) -> SEOInvestigatorState:
        state.mark_source(SOURCE_GEO, False)

        try:
            geo = get_geo_visibility(state.domain, owner=owner)
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s padł dla %s.", self.name, state.domain)
            state.record_error(self.name, exc)
            return state

        if not geo.get("measured"):
            state.geo_visibility_notes.append(
                "Widoczność w wyszukiwarkach AI nie była dotąd mierzona dla tej domeny."
            )
            return state

        state.mark_source(SOURCE_GEO, True)
        if geo.get("measured_at"):
            state.data_timestamps[SOURCE_GEO] = geo["measured_at"]

        sumy = geo.get("totals") or {}
        state.metrics["geo_score"] = geo["overall_score"]
        state.metrics["geo_visible"] = sumy.get("visible")
        state.metrics["geo_total"] = sumy.get("total")
        state.metrics["geo_linked"] = sumy.get("linked")
        state.metrics["geo_mentions"] = sumy.get("mentions")
        state.geo_visibility_notes.append(
            f"Widoczność w wyszukiwarkach AI: {geo['overall_score']}% "
            f"(obecna w {sumy.get('visible', 0)} z {sumy.get('total', 0)} odpowiedzi)."
        )
        if sumy.get("linked") is not None:
            state.geo_visibility_notes.append(
                f"W tym {sumy['linked']} cytowań z odnośnikiem i "
                f"{sumy.get('mentions', 0)} wzmianek bez odnośnika."
            )
        if geo.get("top_competitors"):
            state.geo_visibility_notes.append(
                f"Najczęściej cytowani konkurenci: {geo['top_competitors']}."
            )

        return state


class ReporterAgent:
    """Agent syntezy: składa wnioski specjalistów w raport biznesowy (wywołanie LLM)."""

    name = "ReporterAgent"

    def __init__(self, client=None, model: str = REPORTER_MODEL):
        self._client = client
        self._model = model

    def run(self, state: SEOInvestigatorState) -> SEOInvestigatorState:
        if not state.has_findings:
            state.final_synthesis_report = _fallback_report(state)
            state.record_error(self.name, "Brak ustaleń do syntezy.")
            return state

        try:
            client = self._client or _llm_client()
            odpowiedz = client.responses.create(
                model=self._model,
                input=REPORTER_PROMPT.format(
                    domain=state.domain,
                    technical=_bullets(state.technical_issues),
                    analytics=_bullets(state.analytics_insights),
                    geo=_bullets(state.geo_visibility_notes),
                    errors=_bullets(state.errors) if state.errors else "(brak)",
                ),
            )
            raport = (odpowiedz.output_text or "").strip()
        except Exception as exc:  # noqa: BLE001 - bez raportu LLM zostają ustalenia
            logger.exception("%s nie zdołał wygenerować raportu dla %s.", self.name, state.domain)
            state.record_error(self.name, exc)
            state.final_synthesis_report = _fallback_report(state)
            return state

        # Pusta odpowiedź modelu nie może zostawić użytkownika bez niczego -
        # zebrane ustalenia same w sobie mają wartość.
        state.final_synthesis_report = raport or _fallback_report(state)
        return state


def _query_names(wiersze, limit: int = 3) -> list[str]:
    """Nazwy fraz z wierszy Search Console."""
    return [
        wiersz["query"]
        for wiersz in (wiersze or [])[:limit]
        if wiersz.get("query")
    ]


def _describe_queries(wiersze, limit: int = 3) -> str:
    """Frazy wraz ze zmianą liczby kliknięć, na przykład "enova (-2708 kliknięć)"."""
    opisy = []
    for wiersz in (wiersze or [])[:limit]:
        fraza = wiersz.get("query")
        if not fraza:
            continue
        zmiana = wiersz.get("delta")
        if zmiana:
            opisy.append(f"{fraza} ({zmiana:+d} kliknięć)")
        else:
            opisy.append(fraza)
    return ", ".join(opisy)


def _as_day(wartosc):
    """Dzień z daty lub znacznika czasu - do porównywania świeżości źródeł.

    Źródła oddają raz `date` (koniec serii GA4), raz `datetime` (data rekordu), więc
    porównanie bez tego sprowadzenia wywracałoby się na niezgodnych typach.
    """
    return wartosc.date() if hasattr(wartosc, "date") else wartosc


def _percent_change(current, previous) -> int | None:
    """Zmiana procentowa między okresami - albo None, gdy nie da się jej policzyć.

    Brak punktu odniesienia (zero rok temu) nie jest wzrostem nieskończonym, tylko
    informacją, której nie ma - i tak ją traktujemy.
    """
    if current is None or previous is None or not previous:
        return None
    return round((current - previous) / previous * 100)


def _llm_client():
    """Klient OpenAI - ten sam wzorzec konfiguracji co w pozostałych modułach."""
    from django.conf import settings

    api_key = getattr(settings, "OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Synteza raportu wymaga OPENAI_API_KEY - bez niego raport powstaje "
            "z surowych ustaleń."
        )
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {pozycja}" for pozycja in items) if items else "(brak ustaleń)"


def _fallback_report(state: SEOInvestigatorState) -> str:
    """Raport złożony bez modelu - z samych ustaleń.

    Powstaje, gdy synteza zawiedzie albo nie ma klucza API. Surowa lista jest mniej
    czytelna niż tekst, ale prawdziwa - a badanie nie powinno kończyć się pustką
    tylko dlatego, że jeden krok wymagał sieci.
    """
    sekcje = [f"# Badanie SEO: {state.domain}", ""]

    for naglowek, wpisy in (
        ("## Ustalenia techniczne", state.technical_issues),
        ("## Ruch i widoczność w wyszukiwarce", state.analytics_insights),
        ("## Widoczność w wyszukiwarkach AI", state.geo_visibility_notes),
    ):
        if wpisy:
            sekcje.append(naglowek)
            sekcje.extend(f"- {wpis}" for wpis in wpisy)
            sekcje.append("")

    if state.errors:
        sekcje.append("## Czego nie udało się zbadać")
        sekcje.extend(f"- {blad}" for blad in state.errors)
        sekcje.append("")

    if not state.has_findings:
        sekcje.append("Nie udało się zebrać żadnych ustaleń dla tej domeny.")

    return "\n".join(sekcje).strip()
