#!/usr/bin/env python3
"""Replace the bytes of an existing Google Drive file.

Service accounts have no My Drive storage quota, so they cannot create files
in a user's My Drive. They can update a file the user owns after that file is
shared with the service account as an Editor. This tool only calls
``files.update`` for a file id you already have. It does not create a file
and it does not trash one.

The upload is multipart for files under 5 MiB. Larger files use a resumable
upload. Both paths go through the same ``files.update`` call.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from dataclasses import dataclass
from pathlib import Path

from google_clients import (
    DRIVE_SCOPE,
    GoogleClientError,
    load_credentials as _load_google_credentials,
    short_error as _short,
)

# Multipart upload on Drive accepts up to 5 MiB. At that size and above, use
# the resumable protocol so the request is not rejected for being too large.
RESUMABLE_THRESHOLD_BYTES = 5 * 1024 * 1024

# These win over the platform mime database. ``.csv`` is text/csv here even
# on systems that map it to a spreadsheet type, and ``.md`` is text/markdown
# even when the database has no entry for it.
MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
}


class DriveUpdateError(Exception):
    """User-facing failure. ``exit_code`` 2 is bad input; 1 is auth or API."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class LocalFile:
    path: Path
    size: int
    mime_type: str


@dataclass(frozen=True)
class UpdateResult:
    file_id: str
    web_view_url: str


def guess_mime(path: Path) -> str:
    """Return a media type for ``path`` based on its suffix."""
    explicit = MIME_BY_SUFFIX.get(path.suffix.lower())
    if explicit:
        return explicit
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "application/octet-stream"


def validate_mime(mime: str) -> str:
    """Return a stripped media type, or raise if it is not type/subtype."""
    cleaned = mime.strip()
    if not cleaned:
        raise DriveUpdateError("--mime-type is empty")
    media_type = cleaned.split(";", 1)[0].strip()
    if media_type.count("/") != 1 or " " in media_type:
        raise DriveUpdateError(
            f"--mime-type must look like type/subtype (got {cleaned!r})"
        )
    type_, subtype = media_type.split("/", 1)
    if not type_ or not subtype:
        raise DriveUpdateError(
            f"--mime-type must look like type/subtype (got {cleaned!r})"
        )
    return cleaned


def use_resumable(size: int) -> bool:
    """True when the file is large enough that multipart upload is rejected."""
    return size >= RESUMABLE_THRESHOLD_BYTES


def inspect_local_file(path: Path, mime_type: str | None) -> LocalFile:
    """Check that ``path`` is a readable file and decide its media type."""
    if not path.exists():
        raise DriveUpdateError(f"File not found: {path}")
    if not path.is_file():
        raise DriveUpdateError(f"Path is not a file: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DriveUpdateError(f"Could not read file {path}: {exc}") from exc
    return LocalFile(
        path=path,
        size=size,
        mime_type=mime_type if mime_type is not None else guess_mime(path),
    )


def drive_view_url(file_id: str, payload: dict | None = None) -> str:
    if payload and payload.get("webViewLink"):
        return str(payload["webViewLink"])
    return f"https://drive.google.com/file/d/{file_id}/view"


def access_hint(status: int | None, service_account_email: str | None) -> str:
    """Extra sentence for auth and permission failures. Empty if not applicable."""
    if status not in (401, 403, 404):
        return ""
    if service_account_email:
        share = (
            f" Share the file with {service_account_email} as an Editor. "
            "Service accounts have no My Drive storage quota, so they cannot "
            "create files there; they can update a file shared with them."
        )
    else:
        share = " Share the file with this account as an Editor."
    if status == 404:
        return (
            " Check the file id. A 404 also means this account cannot see the file."
            + share
        )
    return (
        " Check that the Google Drive API is enabled and that these credentials "
        "include the Drive scope."
        + share
    )


def load_credentials(path: str | None, scopes: list[str]):
    """Load a service-account file or Application Default Credentials."""
    try:
        return _load_google_credentials(path, scopes, adc_scope_hint="the Drive scope")
    except GoogleClientError as exc:
        raise DriveUpdateError(str(exc), exit_code=exc.exit_code) from exc


def build_drive(creds):
    """Return a Drive v3 client."""
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise DriveUpdateError(
            "Google client libraries are not installed. "
            "Run: pip install -r requirements.txt",
            exit_code=1,
        ) from exc
    try:
        return build("drive", "v3", credentials=creds)
    except Exception as exc:
        raise DriveUpdateError(
            f"Could not create a Google Drive client: {_short(exc)}",
            exit_code=1,
        ) from exc


def _execute(request, creds, file_id: str):
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
        label = (
            f"Google API error ({status}) for file {file_id}"
            if status
            else f"Google API error for file {file_id}"
        )
        hint = access_hint(status, email if isinstance(email, str) else None)
        raise DriveUpdateError(f"{label}: {_short(exc)}.{hint}", exit_code=1) from exc
    except RefreshError as exc:
        raise DriveUpdateError(
            "Google rejected these credentials while refreshing an access token. "
            f"Check the key file, the scopes, and the system clock. Details: {_short(exc)}",
            exit_code=1,
        ) from exc


def _close_media(media) -> None:
    fd = getattr(media, "_fd", None)
    if fd is not None and not fd.closed:
        fd.close()


def update_file(
    drive,
    creds,
    local: LocalFile,
    *,
    file_id: str,
    name: str | None,
) -> UpdateResult:
    """Replace the bytes of ``file_id`` with one ``files.update`` media upload.

    Does not call ``files.create``, ``files.delete``, or trash. ``name`` is
    sent only when the caller asked to change the Drive title.
    """
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise DriveUpdateError(
            "Google client libraries are not installed. "
            "Run: pip install -r requirements.txt",
            exit_code=1,
        ) from exc

    try:
        media = MediaFileUpload(
            str(local.path),
            mimetype=local.mime_type,
            resumable=use_resumable(local.size),
        )
    except OSError as exc:
        raise DriveUpdateError(f"Could not read file {local.path}: {exc}") from exc

    body: dict[str, str] = {}
    if name:
        body["name"] = name
    try:
        payload = _execute(
            drive.files().update(
                fileId=file_id,
                body=body,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            ),
            creds,
            file_id,
        )
    finally:
        _close_media(media)

    if not isinstance(payload, dict):
        raise DriveUpdateError(
            f"Google Drive returned an unexpected response for file {file_id}",
            exit_code=1,
        )
    returned = payload.get("id")
    if returned and returned != file_id:
        raise DriveUpdateError(
            f"Drive returned a different file id ({returned}) than the one "
            f"requested ({file_id}).",
            exit_code=1,
        )
    return UpdateResult(file_id=file_id, web_view_url=drive_view_url(file_id, payload))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drive_update_file",
        description=(
            "Replace the contents of an existing Google Drive file. "
            "The file id and link stay the same. This command only updates "
            "that file id; it does not create or trash a file."
        ),
    )
    parser.add_argument(
        "file",
        help="Local file whose bytes replace the Drive file",
    )
    parser.add_argument(
        "--file-id",
        required=True,
        help="Id of the existing Drive file to update (the id, not the full "
        "URL). The file must already be shared with the caller as an Editor.",
    )
    parser.add_argument(
        "--name",
        help="New Drive title. Omit to leave the title unchanged.",
    )
    parser.add_argument(
        "--mime-type",
        help="Media type for the upload. Default is guessed from the file "
        "name (text/markdown, text/plain, text/csv, application/json, or "
        "the system guess).",
    )
    parser.add_argument(
        "--credentials",
        help="Service-account JSON path. Defaults to Application Default "
        "Credentials (GOOGLE_APPLICATION_CREDENTIALS or gcloud ADC).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print path, size, mime type, and file id. Does not call Google.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.file_id is None:
        raise DriveUpdateError("--file-id is empty")
    args.file_id = args.file_id.strip()
    if not args.file_id:
        raise DriveUpdateError("--file-id is empty")
    if any(ch.isspace() or ch in "/?#" for ch in args.file_id):
        raise DriveUpdateError(
            "--file-id must be the Drive file id, not a full URL "
            f"(got {args.file_id!r})"
        )
    if args.name is not None:
        args.name = args.name.strip()
        if not args.name:
            raise DriveUpdateError("--name is empty")
    if args.mime_type is not None:
        args.mime_type = validate_mime(args.mime_type)


def print_result(result: UpdateResult) -> None:
    print(result.web_view_url)
    print(f"id: {result.file_id}")


def print_dry_run(local: LocalFile, file_id: str, name: str | None) -> None:
    print("Dry run: no Google API calls")
    print(f"path: {local.path}")
    print(f"size: {local.size}")
    print(f"mime: {local.mime_type}")
    print(f"file_id: {file_id}")
    if name:
        print(f"name: {name}")


def _run(args: argparse.Namespace) -> None:
    validate_args(args)
    local = inspect_local_file(Path(args.file).expanduser(), args.mime_type)
    if args.dry_run:
        print_dry_run(local, args.file_id, args.name)
        return
    creds = load_credentials(args.credentials, [DRIVE_SCOPE])
    drive = build_drive(creds)
    result = update_file(
        drive,
        creds,
        local,
        file_id=args.file_id,
        name=args.name,
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
    except DriveUpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
