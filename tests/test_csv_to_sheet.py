"""Unit tests for CSV parsing, range sizing, and bulk Sheets writes."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from csv_to_sheet import (
    DRIVE_SCOPE,
    SHEETS_SCOPE,
    CsvImportError,
    a1_range,
    access_hint,
    column_letter,
    cover_previous,
    ensure_cell_limit,
    main,
    read_csv,
    scopes_for,
    validate_tab,
    write_table,
)


def _http_error(status: int, message: str = "nope"):
    from googleapiclient.errors import HttpError

    class _Resp:
        reason = "error"

        def __init__(self, code: int) -> None:
            self.status = code

    body = f'{{"error": {{"message": "{message}"}}}}'.encode()
    return HttpError(_Resp(status), body)


class TableTests(unittest.TestCase):
    def test_column_letters(self) -> None:
        self.assertEqual(column_letter(1), "A")
        self.assertEqual(column_letter(26), "Z")
        self.assertEqual(column_letter(27), "AA")
        self.assertEqual(column_letter(52), "AZ")
        self.assertEqual(column_letter(53), "BA")

    def test_a1_range_quotes_tab_names(self) -> None:
        self.assertEqual(a1_range("Sheet1", 2, 3), "'Sheet1'!A1:C2")
        self.assertEqual(a1_range("Bob's list", 1, 1), "'Bob''s list'!A1:A1")

    def test_tab_name_rules(self) -> None:
        self.assertEqual(validate_tab("  Data  "), "Data")
        for bad in ("A:B", "bad[name]", "what?", r"a\b", "star*"):
            with self.assertRaises(CsvImportError):
                validate_tab(bad)
        with self.assertRaises(CsvImportError):
            validate_tab("   ")
        with self.assertRaises(CsvImportError):
            validate_tab("x" * 101)

    def test_parse_quotes_newlines_and_jagged_rows(self) -> None:
        text = 'name,note\n"Smith, Ann","line1\nline2"\nonly-one\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "people.csv"
            path.write_text(text, encoding="utf-8")
            table = read_csv(path)
        self.assertEqual(
            table,
            [
                ["name", "note"],
                ["Smith, Ann", "line1\nline2"],
                ["only-one", ""],
            ],
        )

    def test_strips_bom_crlf_and_outer_blank_rows(self) -> None:
        text = "\ufeff\r\na,b\r\n1,2\r\n\r\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.csv"
            path.write_bytes(text.encode("utf-8"))
            table = read_csv(path)
        self.assertEqual(table, [["a", "b"], ["1", "2"]])
        self.assertFalse(table[0][0].startswith("\ufeff"))

    def test_keeps_internal_blank_rows(self) -> None:
        text = "a,b\n,\nc,d\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gap.csv"
            path.write_text(text, encoding="utf-8")
            table = read_csv(path)
        self.assertEqual(table, [["a", "b"], ["", ""], ["c", "d"]])

    def test_empty_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.csv"
            empty.write_text("\n,\n", encoding="utf-8")
            with self.assertRaises(CsvImportError) as ctx:
                read_csv(empty)
            self.assertIn("no data", str(ctx.exception))
            missing = Path(tmp) / "nope.csv"
            with self.assertRaises(CsvImportError) as ctx:
                read_csv(missing)
            self.assertIn("not found", str(ctx.exception))
            with self.assertRaises(CsvImportError) as ctx:
                read_csv(Path(tmp))
            self.assertIn("not a file", str(ctx.exception))

    def test_cover_previous_blanks_leftover_cells(self) -> None:
        covered = cover_previous(
            [["a", "b"]],
            [["x", "y", "z"], ["1", "2", "3"]],
        )
        self.assertEqual(covered, [["a", "b", ""], ["", "", ""]])

    def test_cover_previous_keeps_larger_new_table(self) -> None:
        new = [["a", "b", "c"], ["d", "e", "f"]]
        self.assertEqual(cover_previous(new, [["x"]]), new)

    def test_cell_limit(self) -> None:
        ensure_cell_limit(10, 10)
        with self.assertRaises(CsvImportError):
            ensure_cell_limit(10_000_001, 1)
        with self.assertRaises(CsvImportError) as ctx:
            ensure_cell_limit(1, 18279)
        self.assertIn("columns", str(ctx.exception))

    def test_scopes_and_access_hint(self) -> None:
        self.assertEqual(scopes_for(folder_id=None), [SHEETS_SCOPE])
        self.assertEqual(scopes_for(folder_id="folder"), [SHEETS_SCOPE, DRIVE_SCOPE])
        self.assertIn("spreadsheet id", access_hint(404, "bot@example.com"))
        self.assertNotIn("bot@example.com", access_hint(404, "bot@example.com"))
        hint = access_hint(403, "bot@example.com")
        self.assertIn("bot@example.com", hint)
        self.assertIn("editor", hint)
        self.assertEqual(access_hint(500, None), "")


class CliTests(unittest.TestCase):
    def _write(self, directory: str, name: str, text: str) -> Path:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_dry_run_prints_shape_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "prices.csv", "name,price\nOak,12\n")
            stdout = io.StringIO()
            with patch("csv_to_sheet.load_credentials") as load, patch(
                "csv_to_sheet.build_services"
            ) as build:
                with redirect_stdout(stdout):
                    code = main([str(path), "--dry-run", "--tab", "Bob's list"])
            self.assertEqual(code, 0)
            load.assert_not_called()
            build.assert_not_called()
        out = stdout.getvalue().splitlines()
        self.assertEqual(
            out,
            [
                "Dry run: no Google API calls",
                "rows: 2",
                "columns: 2",
                "range: 'Bob''s list'!A1:B2",
                "action: create spreadsheet 'prices'",
            ],
        )

    def test_dry_run_describes_existing_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "rows.csv", "h\n1\n2\n")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        str(path),
                        "--dry-run",
                        "--spreadsheet-id", "abc123",
                        "--tab", "Shortlist",
                        "--mode", "replace",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertIn("rows: 3", stdout.getvalue())
        self.assertIn("columns: 1", stdout.getvalue())
        self.assertIn(
            "action: replace tab 'Shortlist' on spreadsheet abc123",
            stdout.getvalue(),
        )

    def test_input_errors(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            missing = main(["/no/such/file.csv", "--dry-run"])
        self.assertEqual(missing, 2)
        self.assertIn("not found", stderr.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            empty = self._write(tmp, "empty.csv", "")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(empty), "--dry-run"])
            self.assertEqual(code, 2)
            self.assertIn("no data", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(empty), "--mode", "append"])
            self.assertEqual(code, 2)
            self.assertIn("--spreadsheet-id", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [str(empty), "--spreadsheet-id", "abc", "--folder-id", "folder"]
                )
            self.assertEqual(code, 2)
            self.assertIn("--folder-id", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(empty), "--tab", "Bad:Name", "--dry-run"])
            self.assertEqual(code, 2)
            self.assertIn("Tab name", stderr.getvalue())

            creds = Path(tmp) / "missing-key.json"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(empty), "--credentials", str(creds), "--title", "T"])
            # Flag checks pass, then the empty CSV fails before credentials.
            self.assertEqual(code, 2)
            self.assertIn("no data", stderr.getvalue())

            good = self._write(tmp, "ok.csv", "a\n1\n")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(good), "--credentials", str(creds), "--title", "T"])
            self.assertEqual(code, 2)
            self.assertIn("Credentials file not found", stderr.getvalue())

            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(good), "--credentials", str(bad), "--title", "T"])
            self.assertEqual(code, 1)
            self.assertIn("not valid JSON", stderr.getvalue())

    def test_missing_adc_still_mentions_sheets_scope(self) -> None:
        from google.auth.exceptions import DefaultCredentialsError

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "ok.csv", "a\n1\n")
            stderr = io.StringIO()
            with patch(
                "google.auth.default",
                side_effect=DefaultCredentialsError("no creds"),
            ):
                with redirect_stderr(stderr):
                    code = main([str(path), "--title", "T"])
        self.assertEqual(code, 1)
        self.assertIn("the Sheets scope", stderr.getvalue())


class WriteTests(unittest.TestCase):
    def _creds(self):
        creds = MagicMock()
        creds.service_account_email = "bot@example.iam.gserviceaccount.com"
        return creds

    def test_create_is_one_create_and_one_values_update(self) -> None:
        sheets = MagicMock()
        drive = MagicMock()
        sheets.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet123",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet123",
        }
        table = [["h1", "h2", "h3", "h4"]]
        table.extend([[str(r), "b", "c", "d"] for r in range(24)])
        result = write_table(
            table,
            title="Shortlist",
            folder_id=None,
            spreadsheet_id=None,
            tab="Sheet1",
            mode="replace",
            value_input="USER_ENTERED",
            sheets=sheets,
            drive=drive,
            creds=self._creds(),
        )
        create = sheets.spreadsheets.return_value.create
        create.assert_called_once()
        body = create.call_args.kwargs["body"]
        self.assertEqual(body["properties"]["title"], "Shortlist")
        self.assertEqual(body["sheets"][0]["properties"]["title"], "Sheet1")
        self.assertEqual(body["sheets"][0]["properties"]["gridProperties"]["rowCount"], 25)
        self.assertNotIn("values", body)

        values = sheets.spreadsheets.return_value.values.return_value
        values.update.assert_called_once()
        values.append.assert_not_called()
        values.clear.assert_not_called()
        sheets.spreadsheets.return_value.batchUpdate.assert_not_called()
        drive.files.return_value.update.assert_not_called()

        sent = values.update.call_args.kwargs
        self.assertEqual(sent["spreadsheetId"], "sheet123")
        self.assertEqual(sent["range"], "'Sheet1'!A1:D25")
        self.assertEqual(sent["valueInputOption"], "USER_ENTERED")
        self.assertEqual(sent["body"]["values"], table)
        self.assertEqual(result.spreadsheet_id, "sheet123")
        self.assertEqual(result.spreadsheet_url, "https://docs.google.com/spreadsheets/d/sheet123")

    def test_create_moves_into_folder_with_one_drive_update(self) -> None:
        sheets = MagicMock()
        drive = MagicMock()
        sheets.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet123",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet123",
        }
        drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["root"]
        }
        write_table(
            [["a"]],
            title="T",
            folder_id="folder-1",
            spreadsheet_id=None,
            tab="Sheet1",
            mode="replace",
            value_input="RAW",
            sheets=sheets,
            drive=drive,
            creds=self._creds(),
        )
        values = sheets.spreadsheets.return_value.values.return_value
        values.update.assert_called_once()
        self.assertEqual(values.update.call_args.kwargs["valueInputOption"], "RAW")
        drive.files.return_value.update.assert_called_once()
        moved = drive.files.return_value.update.call_args.kwargs
        self.assertEqual(moved["fileId"], "sheet123")
        self.assertEqual(moved["addParents"], "folder-1")
        self.assertEqual(moved["removeParents"], "root")
        self.assertTrue(moved["supportsAllDrives"])

    def test_replace_existing_is_one_update_that_blanks_old_cells(self) -> None:
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.return_value = {
            "spreadsheetId": "abc",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc",
            "sheets": [{"properties": {"title": "Data"}}],
        }
        sheets.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["old", "old2", "old3"], ["x", "y", "z"], ["m", "n", "o"]],
        }
        result = write_table(
            [["n1", "n2"], ["a", "b"]],
            title=None,
            folder_id=None,
            spreadsheet_id="abc",
            tab="Data",
            mode="replace",
            value_input="USER_ENTERED",
            sheets=sheets,
            creds=self._creds(),
        )
        sheets.spreadsheets.return_value.create.assert_not_called()
        sheets.spreadsheets.return_value.batchUpdate.assert_not_called()
        values = sheets.spreadsheets.return_value.values.return_value
        values.get.assert_called_once()
        values.update.assert_called_once()
        values.append.assert_not_called()
        values.clear.assert_not_called()
        sent = values.update.call_args.kwargs
        self.assertEqual(
            sent["body"]["values"],
            [["n1", "n2", ""], ["a", "b", ""], ["", "", ""]],
        )
        self.assertEqual(sent["range"], "'Data'!A1:C3")
        self.assertEqual(result.rows, 3)
        self.assertEqual(result.columns, 3)

    def test_missing_tab_is_one_add_and_one_update(self) -> None:
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.return_value = {
            "spreadsheetId": "abc",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc",
            "sheets": [{"properties": {"title": "Other"}}],
        }
        table = [["h"], ["1"]]
        write_table(
            table,
            title=None,
            folder_id=None,
            spreadsheet_id="abc",
            tab="Data",
            mode="append",
            value_input="RAW",
            sheets=sheets,
            creds=self._creds(),
        )
        batch = sheets.spreadsheets.return_value.batchUpdate
        batch.assert_called_once()
        added = batch.call_args.kwargs["body"]["requests"][0]["addSheet"]
        self.assertEqual(added["properties"]["title"], "Data")
        self.assertEqual(added["properties"]["gridProperties"]["rowCount"], 2)
        values = sheets.spreadsheets.return_value.values.return_value
        values.get.assert_not_called()
        values.append.assert_not_called()
        values.update.assert_called_once()
        self.assertEqual(values.update.call_args.kwargs["body"]["values"], table)

    def test_append_sends_every_row_in_one_call(self) -> None:
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.return_value = {
            "spreadsheetId": "abc",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/abc",
            "sheets": [{"properties": {"title": "Data"}}],
        }
        table = [["h1", "h2"], ["a", "b"], ["c", "d"]]
        write_table(
            table,
            title=None,
            folder_id=None,
            spreadsheet_id="abc",
            tab="Data",
            mode="append",
            value_input="USER_ENTERED",
            sheets=sheets,
            creds=self._creds(),
        )
        values = sheets.spreadsheets.return_value.values.return_value
        values.append.assert_called_once()
        values.update.assert_not_called()
        values.get.assert_not_called()
        sent = values.append.call_args.kwargs
        self.assertEqual(sent["range"], "'Data'!A1")
        self.assertEqual(sent["insertDataOption"], "INSERT_ROWS")
        self.assertEqual(sent["body"]["values"], table)

    def test_permission_error_names_service_account_and_orphan_sheet(self) -> None:
        sheets = MagicMock()
        sheets.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "new1",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/new1",
        }
        sheets.spreadsheets.return_value.values.return_value.update.return_value.execute.side_effect = _http_error(
            403, "The caller does not have permission"
        )
        with self.assertRaises(CsvImportError) as ctx:
            write_table(
                [["a"]],
                title="T",
                folder_id=None,
                spreadsheet_id=None,
                tab="Sheet1",
                mode="replace",
                value_input="RAW",
                sheets=sheets,
                creds=self._creds(),
            )
        message = str(ctx.exception)
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertIn("bot@example.iam.gserviceaccount.com", message)
        self.assertIn("may be empty", message)
        self.assertIn("new1", message)

    def test_missing_spreadsheet_hints_at_the_id(self) -> None:
        sheets = MagicMock()
        sheets.spreadsheets.return_value.get.return_value.execute.side_effect = _http_error(
            404, "Requested entity was not found"
        )
        with self.assertRaises(CsvImportError) as ctx:
            write_table(
                [["a"]],
                title=None,
                folder_id=None,
                spreadsheet_id="missing-id",
                tab="Sheet1",
                mode="replace",
                value_input="RAW",
                sheets=sheets,
                creds=self._creds(),
            )
        self.assertIn("missing-id", str(ctx.exception))
        self.assertIn("spreadsheet id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
