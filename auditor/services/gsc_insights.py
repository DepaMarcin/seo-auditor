from __future__ import annotations

# Ile dodatkowych fraz/podstron wymieniamy w komentarzu obok lidera wzrostu/spadku.
EXTRA_MENTIONS = 2


def _format_extras(items: list[dict], key_name: str) -> str:
    """Formatuje 1-2 dodatkowe pozycje (poza liderem) do wtrącenia w zdaniu,
    np. ', a także "fraza A", "fraza B"'."""
    names = [f'"{item[key_name]}"' for item in items[1 : 1 + EXTRA_MENTIONS]]
    return f", a także {', '.join(names)}" if names else ""


def generate_query_commentary(result: dict) -> str:
    """Generuje tekstowy komentarz PL o frazach kluczowych o największym wpływie na
    wzrost/spadek kliknięć w porównaniu 3M R/R (wynik `GSCService.fetch_yoy_query_performance`)."""
    gainers = result.get("top_gainers") or []
    losers = result.get("top_losers") or []

    if not gainers and not losers:
        return "Brak istotnych zmian w kliknięciach dla poszczególnych fraz w analizowanym okresie."

    parts = []
    if gainers:
        top = gainers[0]
        parts.append(
            f'Największy wzrost kliknięć rok do roku odnotowała fraza "{top["query"]}" '
            f'(+{top["delta"]} kliknięć){_format_extras(gainers, "query")}.'
        )
    if losers:
        top = losers[0]
        parts.append(
            f'Największy spadek kliknięć zanotowała fraza "{top["query"]}" '
            f'({top["delta"]} kliknięć){_format_extras(losers, "query")} - warto zweryfikować, czy nie '
            "straciła pozycji w wynikach wyszukiwania."
        )
    return " ".join(parts)


def generate_page_commentary(result: dict) -> str:
    """Generuje tekstowy komentarz PL o podstronach o największym wpływie na
    wzrost/spadek kliknięć w porównaniu 3M R/R (wynik `GSCService.fetch_yoy_page_performance`)."""
    gainers = result.get("top_gainers") or []
    losers = result.get("top_losers") or []

    if not gainers and not losers:
        return "Brak istotnych zmian w ruchu na poszczególnych podstronach w analizowanym okresie."

    parts = []
    if gainers:
        top = gainers[0]
        parts.append(
            f'Największy wzrost ruchu rok do roku wygenerowała podstrona {top["page"]} '
            f'(+{top["delta"]} kliknięć){_format_extras(gainers, "page")}.'
        )
    if losers:
        top = losers[0]
        parts.append(
            f'Największy spadek ruchu odnotowała podstrona {top["page"]} '
            f'({top["delta"]} kliknięć){_format_extras(losers, "page")} - może to wskazywać na utratę '
            "widoczności tej podstrony lub jej sekcji tematycznej."
        )
    return " ".join(parts)
