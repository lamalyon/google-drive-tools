# google-drive-tools

Bulk-import a local CSV into Google Sheets. One table goes in as one values write, not a loop of cell updates.

Use this when a script (a scraper, an export, an agent) already has a CSV and should drop the whole grid into a spreadsheet. It is not a cell-by-cell writer.

## How the write works

The Sheets API cannot put cell values inside `spreadsheets.create`, so a new file is two calls:

1. `spreadsheets.create` — empty spreadsheet, tab sized to the CSV.
2. `spreadsheets.values.update` — the entire rectangle, starting at A1, in one request.

Updating a spreadsheet that already exists:

| Situation | API calls |
| --- | --- |
| Tab exists, `--mode replace` (default) | One metadata read, one values read, then **one** `values.update`. The update covers both the CSV and any cells already used on that tab. Cells past the new table are blanked in that same write, so old rows do not linger. |
| Tab missing | One `batchUpdate` (`addSheet`), then one `values.update` of the CSV. |
| Tab exists, `--mode append` | One metadata read, then **one** `values.append` of every CSV row (header included). |

Drive "upload the CSV and convert it to a Sheet" is a single call, but it only creates a new file. It cannot write an existing spreadsheet, cannot append, and does not give a reliable tab name. This tool uses the Sheets values API so create, replace, and append share one bulk write path.

Very large sheets can still hit the Sheets request size limit (about 10 MB), the 10 million cell grid limit, or the 18,278 column limit.

## Install

Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies are pinned in `requirements.txt`: `google-api-python-client` and `google-auth`.

## Auth

The CLI never stores credentials. Pass a key you keep locally, or use Application Default Credentials.

Scopes:

- `https://www.googleapis.com/auth/spreadsheets` — always (create and edit sheets).
- `https://www.googleapis.com/auth/drive` — only when you pass `--folder-id`, so the new file can be moved into that folder.

Enable the Google Sheets API on the Cloud project. Enable the Google Drive API as well if you use `--folder-id`.

### Service account

1. Create a service account and a JSON key. Do not commit the key.
2. Either:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
python csv_to_sheet.py data.csv --title "Import"
```

or pass it explicitly (this wins over the env var):

```bash
python csv_to_sheet.py data.csv --credentials /path/to/service-account.json --title "Import"
```

Share the destination with the service account email (`client_email` in the JSON) as an **Editor**:

- Existing spreadsheet: share that file.
- New spreadsheet: pass `--folder-id` for a folder shared with the service account. Otherwise the new file is owned by the service account and lives in its Drive, not yours. The command still prints the URL and id.

You do not need domain-wide delegation.

### User credentials (gcloud ADC)

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive
```

Re-run that command if a later call says the token is missing a scope. If you see a quota-project error:

```bash
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

A spreadsheet created this way is owned by your user.

## Examples

Parse only. No credentials and no Google calls:

```bash
python csv_to_sheet.py shortlist.csv --dry-run
```

```
Dry run: no Google API calls
rows: 12
columns: 6
range: 'Sheet1'!A1:F12
action: create spreadsheet 'shortlist'
```

Create a spreadsheet and write the whole CSV, including the header:

```bash
python csv_to_sheet.py shortlist.csv --title "March shortlist" --folder-id FOLDER_ID
```

Same thing as a module:

```bash
python -m csv_to_sheet shortlist.csv --title "March shortlist"
```

Overwrite an existing tab (default). Creates the tab if it is missing. Replaces values on that tab, including the header row:

```bash
python csv_to_sheet.py shortlist.csv \
  --spreadsheet-id SPREADSHEET_ID \
  --tab Shortlist
```

Append every CSV row after the rows already on the tab. If the CSV still has a header, that header is appended too; drop it from the file first if the tab already has one.

```bash
python csv_to_sheet.py more-rows.csv \
  --spreadsheet-id SPREADSHEET_ID \
  --tab Shortlist \
  --mode append
```

On success the command prints two lines and exits 0:

```
https://docs.google.com/spreadsheets/d/SPREADSHEET_ID
id: SPREADSHEET_ID
```

Numbers and dates are parsed by Sheets (`--value-input USER_ENTERED`, the default). Use `--value-input RAW` to keep every cell as text (leading zeros, values that start with `=`).

## CSV rules

- UTF-8, comma-separated. A leading BOM is fine.
- The first non-empty row is the header and is written.
- Leading and trailing blank lines are dropped. Blank rows in the middle are kept.
- Short rows are padded so the range is a rectangle.

`--dry-run` reports the CSV size. A live replace may write a larger range when it blanks leftover cells.

## Errors

| Exit | When |
| --- | --- |
| 0 | Wrote the sheet, or dry-run succeeded |
| 2 | Missing file, empty CSV, bad flags, missing credentials path, illegal tab name |
| 1 | Auth failure or a Google API error |

Messages go to stderr and start with `error:`. A 403 names the service account email when there is one, and tells you to share the spreadsheet or folder with it. If a spreadsheet is created and a later call fails, the error includes the URL and id.

Tab names cannot contain `: \ / ? * [ ]`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Parsing tests and `--dry-run` do not call Google. Write tests pass a fake Sheets client and assert a single `values.update` or `values.append` for the whole table.
