"""Shared Google credential loading for the CLIs in this repo.

``--credentials`` wins over ``GOOGLE_APPLICATION_CREDENTIALS``. When no path
is passed, ``google.auth.default`` still reads that env var.
"""

from __future__ import annotations

from pathlib import Path

# Full Drive scope. ``drive.file`` does not include files that were only
# shared with a service account, so updating those files needs this scope.
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class GoogleClientError(Exception):
    """Credential or client-library failure.

    ``exit_code`` 2 is a bad local path. ``exit_code`` 1 is auth or a missing
    library.
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def short_error(exc: BaseException, limit: int = 400) -> str:
    """One-line version of an exception, truncated so logs stay readable."""
    text = str(exc).replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def load_credentials(path: str | None, scopes: list[str], *, adc_scope_hint: str):
    """Load a service-account file or Application Default Credentials.

    ``adc_scope_hint`` is the phrase in the missing-ADC error, for example
    ``the Sheets scope`` or ``the Drive scope``.
    """
    cred_path: Path | None = None
    if path:
        cred_path = Path(path).expanduser()
        if not cred_path.is_file():
            raise GoogleClientError(
                f"Credentials file not found: {cred_path}",
                exit_code=2,
            )

    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
    except ImportError as exc:
        raise GoogleClientError(
            "Google client libraries are not installed. "
            "Run: pip install -r requirements.txt",
            exit_code=1,
        ) from exc

    try:
        if cred_path is not None:
            creds, _project = google.auth.load_credentials_from_file(
                str(cred_path), scopes=scopes
            )
        else:
            creds, _project = google.auth.default(scopes=scopes)
    except DefaultCredentialsError as exc:
        if cred_path is not None:
            detail = short_error(exc)
            if "not a valid json" in detail.lower():
                raise GoogleClientError(
                    f"Credentials file is not valid JSON: {cred_path}",
                    exit_code=1,
                ) from exc
            raise GoogleClientError(
                f"Could not load Google credentials from {cred_path}: {detail}",
                exit_code=1,
            ) from exc
        raise GoogleClientError(
            "Could not find Google credentials. Pass --credentials, set "
            "GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON file, "
            "or run `gcloud auth application-default login` with "
            f"{adc_scope_hint}. Details: {short_error(exc)}",
            exit_code=1,
        ) from exc
    except (GoogleAuthError, ValueError, OSError) as exc:
        raise GoogleClientError(
            f"Could not load Google credentials: {short_error(exc)}",
            exit_code=1,
        ) from exc
    return creds
