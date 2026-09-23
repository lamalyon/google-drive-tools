# google-drive-tools

Two command-line tools:

- `csv_to_sheet.py` — bulk-import a local CSV into a spreadsheet. One table goes in as one values write, not a loop of cell updates.
- `drive_update_file.py` — replace the bytes of an existing Drive file and keep the same file id and link.

Use `csv_to_sheet.py` when a script (a scraper, an export, an agent) already has a CSV and should drop the whole grid into a spreadsheet. It is not a cell-by-cell writer.

Use `drive_update_file.py` when the artifact is a file — a Markdown brief, notes, JSON, or a CSV that should stay a file — and the Drive link must stay the same. It does not turn the file into spreadsheet cells. See [Update a Drive file in place](#update-a-drive-file-in-place).

## Import a CSV into Sheets

### How the write works

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

Dependencies are pinned in `requirements.txt`: `google-api-python-client` and `google-auth`. Both CLIs use those libraries. Nothing else is required.

## Auth

Neither CLI stores credentials. Pass a key you keep locally, or use Application Default Credentials. Do not commit the key, and do not upload it to Drive.

Scopes:

- `https://www.googleapis.com/auth/spreadsheets` — `csv_to_sheet.py` (create and edit sheets).
- `https://www.googleapis.com/auth/drive` — `csv_to_sheet.py` only when you pass `--folder-id`, so the new spreadsheet can be moved into that folder. `drive_update_file.py` always uses this scope.

`drive_update_file.py` uses the full Drive scope on purpose. The narrower `drive.file` scope only covers files the app created or that a user opened with the app. A file you share with a service account from the Drive sharing dialog is not in that set, so an update returns 404. The `drive` scope can update a file shared with the caller as an Editor.

Enable the Google Sheets API on the Cloud project for `csv_to_sheet.py`. Enable the Google Drive API for `drive_update_file.py`, and for `csv_to_sheet.py` if you use `--folder-id`.

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
- Existing Drive file (Markdown and other files): share that file, then update it with `drive_update_file.py`. Service accounts have no My Drive storage quota, so they cannot create a new file in your My Drive. Updating a file you own works. See [Update a Drive file in place](#update-a-drive-file-in-place).

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

## Sheet examples

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

## Sheet import errors

| Exit | When |
| --- | --- |
| 0 | Wrote the sheet, or dry-run succeeded |
| 2 | Missing file, empty CSV, bad flags, missing credentials path, illegal tab name |
| 1 | Auth failure or a Google API error |

Messages go to stderr and start with `error:`. A 403 names the service account email when there is one, and tells you to share the spreadsheet or folder with it. If a spreadsheet is created and a later call fails, the error includes the URL and id.

Tab names cannot contain `: \ / ? * [ ]`.

## Update a Drive file in place

`drive_update_file.py` calls Drive `files.update` with a media upload for a file id you already have. The id and the link stay the same. The command does not create a file and does not trash one.

| You have | Use |
| --- | --- |
| Rows that should become spreadsheet cells (replace a tab, append rows, or create a Sheet) | `csv_to_sheet.py` |
| A file whose bytes should change and whose Drive link must not (a Markdown brief, text, JSON, or a CSV kept as a file) | `drive_update_file.py` |

Service accounts have no My Drive storage quota. Creating a file in a user's My Drive fails for them. This path avoids that:

1. As your user (the Drive website, or user credentials), create or upload the file once. An empty `BRIEF.md` is enough. Copy the file id from the link `https://drive.google.com/file/d/FILE_ID/view`. Pass that id, not the whole URL.
2. Share that file with the service account email (`client_email` in the JSON key) as an **Editor**.
3. Refresh the content in place whenever the local file changes.

The target should be a normal Drive file (for example a `.md` file stored as `text/markdown`). This tool replaces that file's bytes. It does not edit the body of a native Google Doc. Spreadsheet cells still go through `csv_to_sheet.py`.

Check the local file without credentials and without calling Google:

```bash
python drive_update_file.py BRIEF.md --file-id FILE_ID --dry-run
```

```
Dry run: no Google API calls
path: BRIEF.md
size: 128
mime: text/markdown
file_id: FILE_ID
```

Upload the new bytes. `--credentials` wins over `GOOGLE_APPLICATION_CREDENTIALS`. Omit it to use Application Default Credentials.

```bash
python drive_update_file.py BRIEF.md \
  --file-id FILE_ID \
  --credentials /path/to/service-account.json
```

Same thing as a module:

```bash
python -m drive_update_file BRIEF.md --file-id FILE_ID
```

Leave the Drive title as it is, or set a new one with `--name`. The default media type comes from the file name (`text/markdown` for `.md` and `.markdown`, `text/plain` for `.txt`, `text/csv` for `.csv`, `application/json` for `.json`). Override it with `--mime-type` when the name is not enough.

```bash
python drive_update_file.py BRIEF.md \
  --file-id FILE_ID \
  --name "March brief"
```

Files under 5 MiB upload as multipart. Larger files use a resumable upload. Both are one `files.update` of the same file id.

On success the command prints two lines and exits 0. The first line is the file's `webViewLink` when Drive returns one:

```
https://drive.google.com/file/d/FILE_ID/view
id: FILE_ID
```

Keep keys off Drive and out of git. The command replaces whatever file id you pass with the local bytes, so point it at the brief, not at a service-account JSON file or a `.env`.

| Exit | When |
| --- | --- |
| 0 | Updated the file, or dry-run succeeded |
| 2 | Missing local file, empty `--file-id`, bad flags, missing credentials path |
| 1 | Auth failure or a Google API error |

Messages go to stderr and start with `error:`. A 403 or 404 names the service account email when there is one, and tells you to share the file with it as an Editor.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Parsing tests and `--dry-run` do not call Google. Sheet write tests pass a fake Sheets client and assert a single `values.update` or `values.append` for the whole table. Drive tests pass a fake Drive client and assert a single `files.update` media upload for the given file id.
