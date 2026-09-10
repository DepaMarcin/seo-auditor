"""Eksport raportu do Google Sheets na koncie użytkownika.

Korzysta z tych samych poświadczeń OAuth co GA4 i Search Console (token odświeżania
trzymany zaszyfrowany - patrz `auditor.services.crypto`), rozszerzonych o zakres
`https://www.googleapis.com/auth/spreadsheets`.

WAŻNE: konta połączone przed dodaniem tego zakresu mają token BEZ uprawnienia do
arkuszy. Google odpowiada wtedy błędem 403/insufficient scope - rozpoznajemy to
i podnosimy `MissingSheetsScopeError`, żeby interfejs mógł poprosić o ponowne
połączenie konta zamiast pokazywać surowy błąd API.
"""
from __future__ import annotations

import logging

import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .exporter import Sheet

logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_HTTP_TIMEOUT_SECONDS = 60

# Google odrzuca pojedyncze żądanie powyżej ok. 10 MB; matryca techniczna przy 5
# szablonach ma kilkaset wierszy, więc wysyłamy dane porcjami po tyle wierszy.
MAX_ROWS_PER_REQUEST = 500


class SheetsExportError(Exception):
    """Nie udało się utworzyć arkusza w Google Sheets."""


class MissingSheetsScopeError(SheetsExportError):
    """Token użytkownika nie obejmuje zakresu `spreadsheets` - konieczne ponowne połączenie."""


class GoogleSheetsService:
    """Tworzy nowy arkusz Google z gotowym raportem i zwraca jego adres."""

    def _build_service(self, credentials: Credentials):
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=SHEETS_HTTP_TIMEOUT_SECONDS)
        )
        return build("sheets", "v4", http=authorized_http, cache_discovery=False)

    def create_report(self, credentials: Credentials, title: str, sheets: list[Sheet]) -> str:
        """Tworzy arkusz z jedną zakładką na każdy `Sheet` i zwraca URL do otwarcia.

        Cała struktura (zakładki + nagłówki) powstaje w JEDNYM żądaniu tworzącym
        skoroszyt, a dane dopisujemy porcjami - zamiast tworzyć pusty arkusz i dodawać
        zakładki pojedynczo, co kosztowałoby kilka dodatkowych round-tripów.
        """
        try:
            service = self._build_service(credentials)
            spreadsheet = service.spreadsheets().create(
                body={
                    "properties": {"title": title},
                    "sheets": [
                        {
                            "properties": {
                                "title": sheet.title[:100],
                                "gridProperties": {"frozenRowCount": 1},
                            }
                        }
                        for sheet in sheets
                    ],
                },
                fields="spreadsheetId,spreadsheetUrl,sheets.properties",
            ).execute()
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        except Exception as exc:
            logger.exception("Nie udało się utworzyć arkusza Google Sheets.")
            raise SheetsExportError("Nie udało się utworzyć arkusza w Google Sheets.") from exc

        spreadsheet_id = spreadsheet["spreadsheetId"]

        try:
            self._fill_sheets(service, spreadsheet_id, sheets)
            self._format_headers(service, spreadsheet_id, spreadsheet.get("sheets", []))
        except HttpError as exc:
            raise self._translate_error(exc) from exc
        except Exception as exc:
            logger.exception("Nie udało się wypełnić arkusza %s danymi raportu.", spreadsheet_id)
            raise SheetsExportError("Arkusz powstał, ale nie udało się wypełnić go danymi.") from exc

        return spreadsheet.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    def _fill_sheets(self, service, spreadsheet_id: str, sheets: list[Sheet]) -> None:
        """Wypełnia zakładki danymi, dzieląc duże tabele na porcje."""
        for sheet in sheets:
            table = sheet.as_table()
            for offset in range(0, len(table), MAX_ROWS_PER_REQUEST):
                chunk = table[offset:offset + MAX_ROWS_PER_REQUEST]
                # A1 notation liczy wiersze od 1, stąd przesunięcie o jeden.
                start_row = offset + 1
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{sheet.title[:100]}'!A{start_row}",
                    valueInputOption="RAW",
                    body={"values": [[_cell(value) for value in row] for row in chunk]},
                ).execute()

    def _format_headers(self, service, spreadsheet_id: str, sheet_properties: list[dict]) -> None:
        """Pogrubia wiersz nagłówka w każdej zakładce - jedno żądanie zbiorcze."""
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": entry["properties"]["sheetId"],
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.23},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            }
            for entry in sheet_properties
            if entry.get("properties", {}).get("sheetId") is not None
        ]
        if requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()

    def _translate_error(self, exc: HttpError) -> SheetsExportError:
        """Zamienia błąd API na wyjątek zrozumiały dla interfejsu."""
        status = getattr(exc.resp, "status", None)
        details = str(exc)

        if status == 403 and ("insufficient" in details.lower() or "scope" in details.lower()):
            logger.info("Token Google bez zakresu spreadsheets - wymagane ponowne połączenie konta.")
            return MissingSheetsScopeError(
                "Połączone konto Google nie ma uprawnienia do tworzenia arkuszy. "
                "Połącz konto ponownie, żeby przyznać dostęp do Google Sheets."
            )
        if status == 401:
            return MissingSheetsScopeError(
                "Sesja Google wygasła. Połącz konto ponownie, żeby wyeksportować raport."
            )

        logger.warning("Google Sheets API zwróciło błąd HTTP %s.", status)
        return SheetsExportError("Google Sheets odrzuciło żądanie utworzenia raportu.")


def _cell(value) -> str | int | float:
    """Wartość komórki w formacie akceptowanym przez API (liczby zostają liczbami)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    return "" if value is None else str(value)
