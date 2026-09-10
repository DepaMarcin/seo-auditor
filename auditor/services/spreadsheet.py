"""Zapis raportu (`auditor.services.exporter.Sheet`) do pliku XLSX albo CSV.

Oddzielone od `exporter.py` celowo: tam powstaje treść raportu, tutaj wyłącznie jej
serializacja do konkretnego formatu. Dzięki temu dodanie kolejnego formatu nie wymaga
dotykania logiki budowania danych.
"""
from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .exporter import STATUS_LABELS, Sheet

# Kolory nagłówka i statusów - odpowiedniki zmiennych --error/--warning/--ok z interfejsu,
# żeby arkusz czytało się tak samo jak raport na ekranie.
HEADER_FILL = PatternFill("solid", fgColor="1E293B")
HEADER_FONT = Font(bold=True, color="F8FAFC")
STATUS_FILLS = {
    STATUS_LABELS["error"]: PatternFill("solid", fgColor="FEE2E2"),
    STATUS_LABELS["warning"]: PatternFill("solid", fgColor="FEF3C7"),
    STATUS_LABELS["ok"]: PatternFill("solid", fgColor="DCFCE7"),
}

# Granice szerokości kolumn - bez nich długie rekomendacje AI rozciągnęłyby kolumnę
# na kilkaset znaków i arkusz byłby nieczytelny.
MIN_COLUMN_WIDTH = 12
MAX_COLUMN_WIDTH = 60


def build_xlsx(sheets: list[Sheet]) -> bytes:
    """Buduje skoroszyt XLSX z jedną zakładką na każdy `Sheet`."""
    workbook = Workbook()
    # Nowy skoroszyt ma jeden pusty arkusz - używamy go jako pierwszej zakładki.
    workbook.remove(workbook.active)

    for sheet in sheets:
        worksheet = workbook.create_sheet(title=_safe_title(sheet.title))
        worksheet.append(sheet.headers)
        for row in sheet.rows:
            worksheet.append(row)

        _style_header(worksheet, len(sheet.headers))
        _style_status_column(worksheet, sheet)
        _autosize_columns(worksheet, sheet)
        # Zamrożony nagłówek: przy kilkuset wierszach matrycy nazwy kolumn muszą zostać widoczne.
        worksheet.freeze_panes = "A2"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_csv(sheets: list[Sheet]) -> str:
    """Buduje jeden plik CSV ze wszystkimi zakładkami.

    CSV nie zna pojęcia zakładek, więc sekcje rozdzielamy nagłówkiem "### <tytuł> ###"
    i pustym wierszem - arkusze kalkulacyjne wczytają to jako jedną tabelę, w której
    granice sekcji pozostają czytelne dla człowieka.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")

    for index, sheet in enumerate(sheets):
        if index:
            writer.writerow([])
        writer.writerow([f"### {sheet.title} ###"])
        writer.writerow(sheet.headers)
        writer.writerows(sheet.rows)

    return buffer.getvalue()


def _safe_title(title: str) -> str:
    """Tytuł zakładki zgodny z ograniczeniami Excela (31 znaków, bez znaków zabronionych)."""
    for forbidden in r"[]:*?/\\":
        title = title.replace(forbidden, "-")
    return title[:31]


def _style_header(worksheet, column_count: int) -> None:
    for column in range(1, column_count + 1):
        cell = worksheet.cell(row=1, column=column)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _style_status_column(worksheet, sheet: Sheet) -> None:
    """Koloruje komórki kolumny "Status" wg wagi problemu."""
    if "Status" not in sheet.headers:
        return

    column_index = sheet.headers.index("Status") + 1
    for row_index in range(2, len(sheet.rows) + 2):
        cell = worksheet.cell(row=row_index, column=column_index)
        fill = STATUS_FILLS.get(str(cell.value))
        if fill:
            cell.fill = fill


def _autosize_columns(worksheet, sheet: Sheet) -> None:
    for column_index, header in enumerate(sheet.headers, start=1):
        longest = len(str(header))
        for row in sheet.rows:
            if column_index <= len(row):
                longest = max(longest, len(str(row[column_index - 1])))
        width = max(MIN_COLUMN_WIDTH, min(longest + 2, MAX_COLUMN_WIDTH))
        worksheet.column_dimensions[get_column_letter(column_index)].width = width
