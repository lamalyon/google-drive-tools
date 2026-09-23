#!/usr/bin/env python3
"""Bulk-import a local CSV into Google Sheets.

A new spreadsheet is one ``spreadsheets.create`` plus one ``values.update`` of
the whole table. An existing tab is one ``values.update`` of the rectangle
that covers both the CSV and cells already on that tab, so leftovers are
blanked in the same write. A missing tab is added with one ``batchUpdate``,
then written the same way. Append is one ``values.append`` of every CSV row.

Drive "upload and convert CSV" is not used. That call can only create a new
file: it cannot update an existing spreadsheet, cannot append, and does not
give a reliable tab name.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

from google_clients import (
    DRIVE_SCOPE,
    GoogleClientError,
    load_credentials as _load_google_credentials,
    short_error as _short,
)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Sheets rejects a spreadsheet larger than this, or wider than A1:ZZZ.
MAX_CELLS = 10_000_000
MAX_COLUMNS = 18278
# Sheet titles cannot contain these characters.
INVALID_TAB_CHARS = set(r"*:\/?[]")
DEFAULT_TAB = "Sheet1"


class CsvImportError(Exception):
    """User-facing failure. ``exit_code`` 2 is bad input; 1 is auth or API."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class WriteResult:
    spreadsheet_id: str
    spreadsheet_url: str
    range_a1: str
    rows: int
    columns: int


def column_letter(index: int) -> str:
    """Convert a 1-based column index to A1 letters (1 -> A, 27 -> AA)."""
    if index < 1:
        raise ValueError(f"column index must be >= 1, got {index}")
    letters: list[str] = []
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


def quote_sheet(title: str) -> str:
    """Quote a tab name for A1 notation. Apostrophes are doubled."""
    return "'" + title.replace("'", "''") + "'"


def a1_range(tab: str, rows: int, cols: int) -> str:
    """Return the A1 range covering a rectangular table starting at A1."""
    if rows < 1 or cols < 1:
        raise ValueError(f"range needs at least one cell, got {rows}x{cols}")
    return f"{quote_sheet(tab)}!A1:{column_letter(cols)}{rows}"


def validate_tab(tab: str) -> str:
    """Return a stripped tab name, or raise if Sheets would reject it."""
    cleaned = tab.strip()
    if not cleaned:
        raise CsvImportError("Tab name is empty")
    bad = sorted({ch for ch in cleaned if ch in INVALID_TAB_CHARS})
    if bad:
        shown = " ".join(bad)
        raise CsvImportError(
            f"Tab name cannot contain {shown} (got {cleaned!r})"
        )
    if len(cleaned) > 100:
        raise CsvImportError("Tab name must be 100 characters or fewer")
    return cleaned


def _row_empty(row: list[str]) -> bool:
    return all(cell == "" for cell in row)


def normalize_table(rows: list[list[str]]) -> list[list[str]]:
    """Drop leading and trailing blank rows and pad to a rectangle.

    Blank rows between data are kept. A file with no remaining cells is an error.
    """
    trimmed = list(rows)
    while trimmed and _row_empty(trimmed[0]):
        trimmed.pop(0)
    while trimmed and _row_empty(trimmed[-1]):
        trimmed.pop()
    if not trimmed or max(len(row) for row in trimmed) == 0:
        raise CsvImportError("CSV has no data")
    width = max(len(row) for row in trimmed)
    return [list(row) + [""] * (width - len(row)) for row in trimmed]


def read_csv(path: Path) -> list[list[str]]:
    """Parse a UTF-8 comma-separated file into a rectangular table of strings."""
    if not path.exists():
        raise CsvImportError(f"CSV not found: {path}")
    if not path.is_file():
        raise CsvImportError(f"CSV path is not a file: {path}")
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            parsed = [list(row) for row in csv.reader(handle)]
    except UnicodeDecodeError as exc:
        raise CsvImportError(
            f"CSV is not valid UTF-8: {path}. Save the file as UTF-8 and retry."
        ) from exc
    except csv.Error as exc:
        raise CsvImportError(f"Could not parse CSV {path}: {exc}") from exc
    except OSError as exc:
        raise CsvImportError(f"Could not read CSV {path}: {exc}") from exc
    return normalize_table(parsed)


def cover_previous(
    new: list[list[str]], previous: list[list[object]]
) -> list[list[str]]:
    """Expand ``new`` so one update overwrites every previously used cell.

    Cells past the new table are blank strings, which clear old values.
    Only the size of ``previous`` is used; old cell contents are not copied.
    """
    prev_rows = len(previous)
    prev_cols = max((len(row) for row in previous), default=0)
    new_rows = len(new)
    new_cols = max((len(row) for row in new), default=0)
    rows = max(prev_rows, new_rows)
    cols = max(prev_cols, new_cols)
    covered: list[list[str]] = []
    for r in range(rows):
        src = new[r] if r < new_rows else []
        covered.append([(src[c] if c < len(src) else "") for c in range(cols)])
    return covered


def ensure_cell_limit(rows: int, cols: int) -> None:
    if cols > MAX_COLUMNS:
        raise CsvImportError(
            f"CSV has {cols} columns. Google Sheets allows at most {MAX_COLUMNS}."
        )
    cells = rows * cols
    if cells > MAX_CELLS:
        raise CsvImportError(
            f"Refusing to write {rows} x {cols} cells ({cells}). "
            f"Google Sheets allows at most {MAX_CELLS} cells."
        )


def spreadsheet_url(spreadsheet_id: str, payload: dict | None = None) -> str:
    if payload and payload.get("spreadsheetUrl"):
        return str(payload["spreadsheetUrl"])
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


def scopes_for(*, folder_id: str | None) -> list[str]:
    """Sheets is always required. Drive is required only to place a new file."""
    scopes = [SHEETS_SCOPE]
    if folder_id:
        scopes.append(DRIVE_SCOPE)
    return scopes


def access_hint(status: int | None, service_account_email: str | None) -> str:
    """Extra sentence for auth and permission failures. Empty if not applicable."""
    if status == 404:
        return " Check the spreadsheet id and that this account can open the file."
    if status in (401, 403):
        share = ""
        if service_account_email:
            share = (
                " If this is a service account, share the spreadsheet or the "
                f"destination folder with {service_account_email} as an editor."
            )
        return (
            " Check that the Google Sheets API is enabled (and the Drive API "
            "if you passed --folder-id) and that these credentials include "
            "the required scope."
            + share
        )
    return ""


def load_credentials(path: str | None, scopes: list[str]):
    """Load a service-account file or Application Default Credentials.

    ``--credentials`` wins over ``GOOGLE_APPLICATION_CREDENTIALS``. When no
    path is passed, ``google.auth.default`` still reads that env var.
    """
    try:
        return _load_google_credentials(path, scopes, adc_scope_hint="the Sheets scope")
    except GoogleClientError as exc:
        raise CsvImportError(str(exc), exit_code=exc.exit_code) from exc


def build_services(creds, *, need_drive: bool):
    """Return ``(sheets_service, drive_service_or_None)``."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise CsvImportError(
            "Google client libraries are not installed. "
            "Run: pip install -r requirements.txt",
            exit_code=1,
        ) from exc
    try:
        sheets = build("sheets", "v4", credentials=creds)
        drive = build("drive", "v3", credentials=creds) if need_drive else None
    except Exception as exc:
        raise CsvImportError(
            f"Could not create a Google API client: {_short(exc)}",
            exit_code=1,
        ) from exc
    return sheets, drive


def _execute(request, creds, spreadsheet_id: str | None = None):
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    try:
        return request.execute()
    except HttpError as exc:
        status = getattr(exc, "status_code", None)
        if status is None and getattr(exc, "resp", None) is not None:
            status = getattr(exc.resp, "status", None)
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        email = getattr(creds, "service_account_email", None)
        where = f" for spreadsheet {spreadsheet_id}" if spreadsheet_id else ""
        label = (
            f"Google API error ({status}){where}"
            if status
            else f"Google API error{where}"
        )
        hint = access_hint(status, email if isinstance(email, str) else None)
        raise CsvImportError(f"{label}: {_short(exc)}.{hint}", exit_code=1) from exc
    except RefreshError as exc:
        raise CsvImportError(
            "Google rejected these credentials while refreshing an access token. "
            f"Check the key file, the scopes, and the system clock. Details: {_short(exc)}",
            exit_code=1,
        ) from exc


def _put_values(
    sheets,
    creds,
    spreadsheet_id: str,
    tab: str,
    table: list[list[str]],
    value_input: str,
) -> str:
    rows = len(table)
    cols = len(table[0])
    ensure_cell_limit(rows, cols)
    range_a1 = a1_range(tab, rows, cols)
    _execute(
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input,
            body={"majorDimension": "ROWS", "values": table},
        ),
        creds,
        spreadsheet_id,
    )
    return range_a1


def _append_values(
    sheets,
    creds,
    spreadsheet_id: str,
    tab: str,
    table: list[list[str]],
    value_input: str,
) -> str:
    rows = len(table)
    cols = len(table[0])
    ensure_cell_limit(rows, cols)
    # The range is where Sheets looks for an existing table. Rows are inserted
    # after that table, not written cell-by-cell.
    search = f"{quote_sheet(tab)}!A1"
    _execute(
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=search,
            valueInputOption=value_input,
            insertDataOption="INSERT_ROWS",
            body={"majorDimension": "ROWS", "values": table},
        ),
        creds,
        spreadsheet_id,
    )
    return a1_range(tab, rows, cols)


def _add_sheet(sheets, creds, spreadsheet_id: str, tab: str, rows: int, cols: int) -> None:
    _execute(
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": tab,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": cols,
                                },
                            }
                        }
                    }
                ]
            },
        ),
        creds,
        spreadsheet_id,
    )


def _move_to_folder(drive, creds, file_id: str, folder_id: str) -> None:
    meta = _execute(
        drive.files().get(fileId=file_id, fields="parents", supportsAllDrives=True),
        creds,
        file_id,
    )
    parents = meta.get("parents") or []
    kwargs = {
        "fileId": file_id,
        "addParents": folder_id,
        "fields": "id, parents",
        "supportsAllDrives": True,
    }
    if parents:
        kwargs["removeParents"] = ",".join(parents)
    _execute(drive.files().update(**kwargs), creds, file_id)


def write_table(
    table: list[list[str]],
    *,
    title: str | None,
    folder_id: str | None,
    spreadsheet_id: str | None,
    tab: str,
    mode: str,
    value_input: str,
    sheets,
    drive=None,
    creds=None,
) -> WriteResult:
    """Write ``table`` in one values call (plus create or addSheet when needed)."""
    if not table or not table[0]:
        raise CsvImportError("CSV has no data")
    rows = len(table)
    cols = len(table[0])
    ensure_cell_limit(rows, cols)

    if not spreadsheet_id:
        created = _execute(
            sheets.spreadsheets().create(
                body={
                    "properties": {"title": title or "CSV import"},
                    "sheets": [
                        {
                            "properties": {
                                "title": tab,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": cols,
                                },
                            }
                        }
                    ],
                },
                fields="spreadsheetId,spreadsheetUrl",
            ),
            creds,
        )
        spreadsheet_id = created["spreadsheetId"]
        url = spreadsheet_url(spreadsheet_id, created)
        try:
            range_a1 = _put_values(sheets, creds, spreadsheet_id, tab, table, value_input)
        except CsvImportError as exc:
            raise CsvImportError(
                f"{exc} A spreadsheet was created but may be empty: {url} (id: {spreadsheet_id})",
                exit_code=exc.exit_code,
            ) from exc
        if folder_id:
            if drive is None:
                raise CsvImportError(
                    "Internal error: Drive client is required for --folder-id",
                    exit_code=1,
                )
            try:
                _move_to_folder(drive, creds, spreadsheet_id, folder_id)
            except CsvImportError as exc:
                raise CsvImportError(
                    f"{exc} Values were written, but the file was not moved into "
                    f"folder {folder_id}. Spreadsheet: {url} (id: {spreadsheet_id})",
                    exit_code=exc.exit_code,
                ) from exc
        return WriteResult(spreadsheet_id, url, range_a1, rows, cols)

    meta = _execute(
        sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="spreadsheetId,spreadsheetUrl,sheets(properties(title))",
        ),
        creds,
        spreadsheet_id,
    )
    url = spreadsheet_url(spreadsheet_id, meta)
    titles = [
        (sheet.get("properties") or {}).get("title")
        for sheet in (meta.get("sheets") or [])
    ]
    if tab not in titles:
        _add_sheet(sheets, creds, spreadsheet_id, tab, rows, cols)
        range_a1 = _put_values(sheets, creds, spreadsheet_id, tab, table, value_input)
        return WriteResult(spreadsheet_id, url, range_a1, rows, cols)

    if mode == "append":
        range_a1 = _append_values(sheets, creds, spreadsheet_id, tab, table, value_input)
        return WriteResult(spreadsheet_id, url, range_a1, rows, cols)

    current = _execute(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=quote_sheet(tab),
            majorDimension="ROWS",
        ),
        creds,
        spreadsheet_id,
    )
    previous = current.get("values") or []
    covered = cover_previous(table, previous)
    range_a1 = _put_values(sheets, creds, spreadsheet_id, tab, covered, value_input)
    covered_rows = len(covered)
    covered_cols = len(covered[0])
    return WriteResult(spreadsheet_id, url, range_a1, covered_rows, covered_cols)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csv_to_sheet",
        description=(
            "Bulk-import a CSV into Google Sheets with one values write "
            "(not row-by-row)."
        ),
    )
    parser.add_argument("csv", help="Path to a local UTF-8 CSV file")
    parser.add_argument(
        "--title",
        help="Title for a new spreadsheet (default: the CSV file name). "
        "Not used with --spreadsheet-id.",
    )
    parser.add_argument(
        "--folder-id",
        help="Drive folder id for a new spreadsheet. The folder must be shared "
        "with the caller. Not used with --spreadsheet-id.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        help="Update this spreadsheet instead of creating one",
    )
    parser.add_argument(
        "--tab",
        default=DEFAULT_TAB,
        help=f"Tab name to write (default: {DEFAULT_TAB}). Created if missing.",
    )
    parser.add_argument(
        "--mode",
        choices=("replace", "append"),
        default="replace",
        help="replace clears the used range and writes the CSV, including the "
        "header (default). append inserts every CSV row after existing rows "
        "and requires --spreadsheet-id.",
    )
    parser.add_argument(
        "--value-input",
        choices=("USER_ENTERED", "RAW"),
        default="USER_ENTERED",
        help="USER_ENTERED lets Sheets parse numbers and dates (default). "
        "RAW writes every cell as text.",
    )
    parser.add_argument(
        "--credentials",
        help="Service-account JSON path. Defaults to Application Default "
        "Credentials (GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the CSV and print row and column counts. Does not call Google.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.spreadsheet_id is not None:
        args.spreadsheet_id = args.spreadsheet_id.strip()
        if not args.spreadsheet_id:
            raise CsvImportError("--spreadsheet-id is empty")
    if args.folder_id is not None:
        args.folder_id = args.folder_id.strip()
        if not args.folder_id:
            raise CsvImportError("--folder-id is empty")
    if args.title is not None:
        args.title = args.title.strip()
        if not args.title:
            raise CsvImportError("--title is empty")
    if args.folder_id and args.spreadsheet_id:
        raise CsvImportError(
            "--folder-id is only used when creating a spreadsheet; omit --spreadsheet-id"
        )
    if args.title and args.spreadsheet_id:
        raise CsvImportError(
            "--title is only used when creating a spreadsheet; omit --spreadsheet-id"
        )
    if args.mode == "append" and not args.spreadsheet_id:
        raise CsvImportError(
            "--mode append needs --spreadsheet-id (a new spreadsheet is always written in full)"
        )
    args.tab = validate_tab(args.tab)


def print_result(result: WriteResult) -> None:
    print(result.spreadsheet_url)
    print(f"id: {result.spreadsheet_id}")


def print_dry_run(table: list[list[str]], args: argparse.Namespace, title: str) -> None:
    rows = len(table)
    cols = len(table[0])
    print("Dry run: no Google API calls")
    print(f"rows: {rows}")
    print(f"columns: {cols}")
    print(f"range: {a1_range(args.tab, rows, cols)}")
    if args.spreadsheet_id:
        print(
            f"action: {args.mode} tab {args.tab!r} on spreadsheet {args.spreadsheet_id}"
        )
    else:
        print(f"action: create spreadsheet {title!r}")


def _run(args: argparse.Namespace) -> None:
    validate_args(args)
    table = read_csv(Path(args.csv).expanduser())
    title = args.title or Path(args.csv).expanduser().stem or "CSV import"
    if args.dry_run:
        print_dry_run(table, args, title)
        return
    scopes = scopes_for(folder_id=args.folder_id)
    creds = load_credentials(args.credentials, scopes)
    sheets, drive = build_services(creds, need_drive=bool(args.folder_id))
    result = write_table(
        table,
        title=title,
        folder_id=args.folder_id,
        spreadsheet_id=args.spreadsheet_id,
        tab=args.tab,
        mode=args.mode,
        value_input=args.value_input,
        sheets=sheets,
        drive=drive,
        creds=creds,
    )
    print_result(result)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    try:
        _run(args)
    except CsvImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
