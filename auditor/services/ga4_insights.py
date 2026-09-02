from __future__ import annotations

# Próg (w dniach) od którego pierwsze 30 dni 365-dniowego okna GA4 faktycznie
# reprezentuje okres sprzed roku, a nie tylko "dalszą przeszłość" - poniżej tego progu
# porównanie rok-do-roku nie ma sensu i analiza spada do miesiąc-do-miesiąca.
MIN_DAYS_FOR_YOY = 395

ORGANIC_CHANNEL = "Organic Search"
PAID_CHANNEL = "Paid Search"
DIRECT_CHANNEL = "Direct"

# Próg wzrostu ruchu płatnego (%), powyżej którego - przy jednoczesnym spadku ruchu
# organicznego - zgłaszamy ostrzeżenie o potencjalnej kanibalizacji płatnej.
PAID_CANNIBALIZATION_THRESHOLD_PCT = 10.0


def _pct_change(previous: float, current: float) -> float | None:
    """Procentowa zmiana `current` względem `previous`, zaokrąglona do 1 miejsca po
    przecinku. Zwraca None, gdy `previous` wynosi 0 - nie ma wtedy sensownej bazy
    procentowej (uniknięcie dzielenia przez zero / sztucznie ogromnych wartości)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _change_sentence(
    subject: str, change: float, rose_word: str, fell_word: str, suffix: str, flat_word: str = "nie zmienił się"
) -> str:
    """Buduje naturalnie brzmiące zdanie o zmianie - dla zmiany zerowej pomija
    powtórzenie "o 0.0%", żeby nie brzmiało nienaturalnie ("nie zmieniła się o 0.0%")."""
    if change > 0:
        return f"{subject} {rose_word} o {change}% {suffix}."
    if change < 0:
        return f"{subject} {fell_word} o {abs(change)}% {suffix}."
    return f"{subject} {flat_word} {suffix}."


def analyze_channel_trends(channels_history: dict, lead_history: dict | None = None) -> dict:
    """Analizuje 12-miesięczne dane wielokanałowe GA4 (`Audit.ga4_channels_history`,
    wynik `GA4OAuthService.fetch_yearly_channel_data`) i wylicza gotowe wnioski SEO.

    `lead_history` to opcjonalnie `{"dates": [...], "events": [...]}` - wynik
    `GA4OAuthService.fetch_event_conversions()["history"]` dla wybranego przez
    użytkownika zdarzenia lead/konwersja (`Audit.ga4_selected_lead_event`).

    Zwraca słownik zapisywany bezpośrednio w `Audit.ga4_insights`, gotowy do
    wyrenderowania w boksie "Automatyczne Wnioski SEO" w `detail.html`:
    {
        "has_data": bool,
        "organic_change_pct": float | None,
        "organic_change_period": "yoy" | "mom" | None,
        "channel_mom_changes": {"Organic Search": float, "Paid Search": float, "Direct": float},
        "cannibalization_warning": bool,
        "lead_change_pct": float | None,
        "lead_trend": "up" | "down" | "flat" | None,
        "summary_points": [str, ...],
    }
    """
    dates = channels_history.get("dates") or []
    channels = channels_history.get("channels") or {}
    organic = channels.get(ORGANIC_CHANNEL, [])
    paid = channels.get(PAID_CHANNEL, [])
    direct = channels.get(DIRECT_CHANNEL, [])

    summary_points: list[str] = []
    insights: dict = {
        "has_data": bool(dates) and bool(organic),
        "organic_change_pct": None,
        "organic_change_period": None,
        "channel_mom_changes": {},
        "cannibalization_warning": False,
        "lead_change_pct": None,
        "lead_trend": None,
        "summary_points": summary_points,
    }

    if not insights["has_data"]:
        summary_points.append("Za mało danych GA4, żeby wyliczyć wnioski o trendzie ruchu organicznego.")
        return insights

    # --- 1. Nagłówkowa zmiana ruchu organicznego: rok-do-roku (gdy mamy ~13 miesięcy
    #     danych - pierwsze 30 dni okna 365-dniowego to wtedy "analogiczne 30 dni rok
    #     temu"), w przeciwnym razie miesiąc-do-miesiąca (poprzednie 30 dni). ---
    current_sum = sum(organic[-30:])
    if len(dates) >= MIN_DAYS_FOR_YOY:
        baseline_sum = sum(organic[:30])
        insights["organic_change_period"] = "yoy"
        period_label = "rok do roku"
    else:
        baseline_sum = sum(organic[-60:-30])
        insights["organic_change_period"] = "mom"
        period_label = "względem poprzednich 30 dni"

    organic_change = _pct_change(baseline_sum, current_sum)
    insights["organic_change_pct"] = organic_change
    if organic_change is not None:
        summary_points.append(_change_sentence("Ruch organiczny", organic_change, "wzrósł", "spadł", f"({period_label})"))

    # --- 2. Dynamika miesiąc-do-miesiąca Organic/Paid/Direct - kontekst dla wykrycia
    #     kanibalizacji płatnej. ---
    mom_changes: dict[str, float] = {}
    for name, series in ((ORGANIC_CHANNEL, organic), (PAID_CHANNEL, paid), (DIRECT_CHANNEL, direct)):
        change = _pct_change(sum(series[-60:-30]), sum(series[-30:]))
        if change is not None:
            mom_changes[name] = change
    insights["channel_mom_changes"] = mom_changes

    organic_mom = mom_changes.get(ORGANIC_CHANNEL)
    paid_mom = mom_changes.get(PAID_CHANNEL)
    if organic_mom is not None and paid_mom is not None:
        if organic_mom < 0 and paid_mom > PAID_CANNIBALIZATION_THRESHOLD_PCT:
            insights["cannibalization_warning"] = True
            summary_points.append(
                f"Ruch organiczny spada ({organic_mom}% m/m), a jednocześnie ruch płatny (Paid Search) "
                f"rośnie o {paid_mom}% m/m - to może wskazywać na kanibalizację płatną (kampanie płatne "
                "przejmują ruch, który wcześniej trafiał organicznie, zamiast realnie zwiększać zasięg)."
            )

    direct_mom = mom_changes.get(DIRECT_CHANNEL)
    if direct_mom is not None:
        summary_points.append(_change_sentence("Ruch bezpośredni (Direct)", direct_mom, "wzrósł", "spadł", "m/m"))

    # --- 3. Trend wybranego zdarzenia lead/konwersja (opcjonalnie, gdy użytkownik
    #     wybrał zdarzenie do śledzenia). ---
    if lead_history and lead_history.get("dates"):
        lead_values = lead_history.get("events", [])
        lead_change = _pct_change(sum(lead_values[-60:-30]), sum(lead_values[-30:]))
        insights["lead_change_pct"] = lead_change
        if lead_change is not None:
            insights["lead_trend"] = "up" if lead_change > 0 else "down" if lead_change < 0 else "flat"
            summary_points.append(
                _change_sentence(
                    "Liczba konwersji z ruchu organicznego", lead_change, "wzrosła", "spadła", "m/m",
                    flat_word="nie zmieniła się",
                )
            )
        else:
            summary_points.append("Za mało danych o wybranym zdarzeniu, żeby wyliczyć jego trend.")

    if not summary_points:
        summary_points.append("Brak istotnych zmian trendu w analizowanym okresie.")

    return insights
