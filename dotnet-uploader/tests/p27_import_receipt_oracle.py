"""Synthetic P27 oracle; extract pure legacy controls without importing the app."""
import ast
from datetime import date, datetime
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
from xml.etree import ElementTree
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook


def controls():
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "services/upload_reconciliation.py").read_text(encoding="utf-8"))
    wanted = {"stored_reconciliation", "footing_columns"}
    pure = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name in wanted], type_ignores=[])
    scope = dict(datetime=datetime, ZoneInfo=ZoneInfo, Decimal=Decimal,
                 localcontext=localcontext,
                 resolve_document_column=lambda columns: columns[0])
    exec(compile(pure, "legacy_reconciliation", "exec"), scope)
    columns = [{"column_name": "Document"},
               {"column_name": "Amount", "reconciliation_role": "footing"}]
    expected = dict(row_count=3, document_count=2, missing_document_rows=0,
                    footing_totals={"Amount": "0.30"})
    vectors = []
    for complete, unavailable, actual in [
        (True, False, expected),
        (True, False, dict(expected, footing_totals={"Amount": "0.33"})),
        (True, False, dict(expected, missing_document_rows=1)),
        (True, True, None), (False, False, expected), (False, True, None),
        (True, False, dict(expected, row_count=4)),
        (True, False, dict(expected, document_count=3)),
    ]:
        result = scope["stored_reconciliation"]({"approved_detail": expected}, columns,
                    {"complete_period": complete}, actual, unavailable=unavailable)
        difference = result["rows"][2]["difference"]
        if difference is not None:
            difference = format(Decimal(difference).normalize(), "f")
        vectors.append(dict(complete=complete, unavailable=unavailable, stored=actual,
                            status=result["status"], difference=difference))
    return vectors


def generate(directory):
    directory.mkdir(parents=True, exist_ok=False)
    book = Workbook()
    book.remove(book.active)
    for name, second in [("Base", 100), ("Changed", 120)]:
        sheet = book.create_sheet(name)
        sheet.append(["Document", "Date", "Amount", "Memo", "Balance", "Extra"])
        for doc, day, amount in [("V1", 3, 100), ("V1", 3, second), ("V2", 8, 50)]:
            sheet.append([doc, date(2026, 4, day), amount, "=SOURCE()", f"{amount} DR", None])
            sheet.cell(sheet.max_row, 4).data_type = "s"
            sheet.cell(sheet.max_row, 2).number_format = "dd/mm/yyyy"
        sheet.append(["Total", None, 150 + second, None, None, None])
    book.save(directory / "plans.xlsx")
    (directory / "oracle.json").write_text(json.dumps({"controls": controls()}), encoding="utf-8")


def verify(path):
    book = load_workbook(path, data_only=False)
    assert book.sheetnames == ["Summary", "Reconciliation", "Issues", "Previous rows", "Stored rows"]
    for sheet in book:
        assert sheet.freeze_panes == "A6"
        assert sheet.auto_filter.ref.startswith("A5:")
        assert sheet.page_setup.orientation == "landscape"
        assert sheet["A5"].fill.fgColor.rgb == "FFE2EFDA"
        for row in sheet:
            assert all(cell.data_type != "f" for cell in row)
    previous, stored = book["Previous rows"], book["Stored rows"]
    assert previous.max_row == 7 and stored.max_row == 7, "Both repeated V1 lines must be retained"
    assert [previous.cell(row, 6).value for row in (6, 7)] == [100, 100]
    assert [stored.cell(row, 6).value for row in (6, 7)] == [100, 120]
    for sheet in (previous, stored):
        for row in (6, 7):
            assert sheet.cell(row, 7).value == "=SOURCE()"
            assert sheet.cell(row, 7).data_type == "s"
            assert sheet.cell(row, 3).value == datetime(2026, 4, 3)
            assert sheet.cell(row, 5).value == "03/04/2026"
    reconciliation = book["Reconciliation"]
    assert reconciliation["C8"].value == 270
    assert reconciliation["D8"].value == 0
    assert "not manager-reviewed" in book["Summary"]["B16"].value
    print("P27 workbook values, repeated lines, safe strings, dates and controls verified")


def verify_edges(path):
    book = load_workbook(path, data_only=False)
    sheet = book["Stored rows"]
    assert sheet["D6"].value == "0000123" and sheet["D6"].data_type == "s"
    assert sheet["E6"].value == "=HYPERLINK(unsafe)" and sheet["E6"].data_type == "s"
    assert sheet["F6"].value == "123456789012345678901234567890.12345"
    assert sheet["F6"].data_type == "s", "Long exact numbers must not be rounded by Excel"
    assert sheet["F7"].value == "1e-1000" and sheet["F7"].data_type == "s"
    assert sheet["H6"].value == "-00123" and sheet["I6"].value == "+SUM(A1:A9)"
    with ZipFile(path) as archive:
        tree = ElementTree.fromstring(archive.read("xl/worksheets/sheet5.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        assert tree.find(".//s:c[@r='G6']/s:is/s:t", ns).text == "_x005F_x000D_ & < >"
    print("P27 literal strings, OOXML escapes, leading zeroes and exact long amounts verified")


if __name__ == "__main__":
    if sys.argv[1] == "--verify":
        verify(Path(sys.argv[2]))
    elif sys.argv[1] == "--verify-edges":
        verify_edges(Path(sys.argv[2]))
    else:
        generate(Path(sys.argv[1]))
