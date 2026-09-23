"""Unit tests for in-place Drive file updates.

No test calls Google. Upload tests pass a fake Drive client.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.auth.exceptions import DefaultCredentialsError

from drive_update_file import (
    DRIVE_SCOPE,
    RESUMABLE_THRESHOLD_BYTES,
    DriveUpdateError,
    LocalFile,
    access_hint,
    guess_mime,
    main,
    update_file,
    use_resumable,
    validate_mime,
)
from googleapiclient.http import MediaFileUpload


def _http_error(status: int, message: str = "nope"):
    from googleapiclient.errors import HttpError

    class _Resp:
        reason = "error"

        def __init__(self, code: int) -> None:
            self.status = code

    body = f'{{"error": {{"message": "{message}"}}}}'.encode()
    return HttpError(_Resp(status), body)


class MimeTests(unittest.TestCase):
    def test_known_suffixes(self) -> None:
        self.assertEqual(guess_mime(Path("BRIEF.md")), "text/markdown")
        self.assertEqual(guess_mime(Path("BRIEF.MD")), "text/markdown")
        self.assertEqual(guess_mime(Path("notes.markdown")), "text/markdown")
        self.assertEqual(guess_mime(Path("plain.txt")), "text/plain")
        self.assertEqual(guess_mime(Path("rows.csv")), "text/csv")
        self.assertEqual(guess_mime(Path("data.JSON")), "application/json")

    def test_unknown_suffix_falls_back(self) -> None:
        self.assertEqual(guess_mime(Path("blob.zzz-not-a-type")), "application/octet-stream")
        self.assertEqual(guess_mime(Path("README")), "application/octet-stream")

    def test_mime_override_rules(self) -> None:
        self.assertEqual(validate_mime(" text/markdown "), "text/markdown")
        self.assertEqual(
            validate_mime("text/plain; charset=utf-8"),
            "text/plain; charset=utf-8",
        )
        with self.assertRaises(DriveUpdateError):
            validate_mime("   ")
        with self.assertRaises(DriveUpdateError):
            validate_mime("markdown")
        with self.assertRaises(DriveUpdateError):
            validate_mime("text/")

    def test_resumable_threshold(self) -> None:
        self.assertFalse(use_resumable(0))
        self.assertFalse(use_resumable(RESUMABLE_THRESHOLD_BYTES - 1))
        self.assertTrue(use_resumable(RESUMABLE_THRESHOLD_BYTES))
        self.assertTrue(use_resumable(RESUMABLE_THRESHOLD_BYTES + 1))


class CliTests(unittest.TestCase):
    def test_dry_run_prints_path_size_mime_and_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BRIEF.md"
            path.write_text("# Hello\n", encoding="utf-8")
            size = path.stat().st_size
            stdout = io.StringIO()
            with patch("drive_update_file.load_credentials") as load, patch(
                "drive_update_file.build_drive"
            ) as build:
                with redirect_stdout(stdout):
                    code = main(
                        [
                            str(path),
                            "--file-id",
                            "  abc123  ",
                            "--dry-run",
                            "--credentials",
                            "/no/such/key.json",
                        ]
                    )
            self.assertEqual(code, 0)
            load.assert_not_called()
            build.assert_not_called()
        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "Dry run: no Google API calls",
                f"path: {path}",
                f"size: {size}",
                "mime: text/markdown",
                "file_id: abc123",
            ],
        )

    def test_dry_run_mime_override_and_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BRIEF.md"
            path.write_text("x", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        str(path),
                        "--file-id",
                        "abc",
                        "--dry-run",
                        "--mime-type",
                        "application/json",
                        "--name",
                        "  Weekly brief  ",
                    ]
                )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("mime: application/json", text)
        self.assertIn("name: Weekly brief", text)
        self.assertIn("size: 1", text)

    def test_input_errors(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["brief.md"])
        self.assertEqual(code, 2)
        self.assertIn("--file-id", stderr.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["brief.md", "--file-id", "   ", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("--file-id is empty", stderr.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [
                    "brief.md",
                    "--file-id",
                    "https://drive.google.com/file/d/abc123/view",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("not a full URL", stderr.getvalue())

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["/no/such/BRIEF.md", "--file-id", "abc", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("File not found", stderr.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([tmp, "--file-id", "abc", "--dry-run"])
            self.assertEqual(code, 2)
            self.assertIn("not a file", stderr.getvalue())

            path = Path(tmp) / "BRIEF.md"
            path.write_text("# ok\n", encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main([str(path), "--file-id", "abc", "--name", "  "])
            self.assertEqual(code, 2)
            self.assertIn("--name is empty", stderr.getvalue())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [str(path), "--file-id", "abc", "--mime-type", "not-a-mime"]
                )
            self.assertEqual(code, 2)
            self.assertIn("--mime-type", stderr.getvalue())

            missing_key = Path(tmp) / "missing-key.json"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [str(path), "--file-id", "abc", "--credentials", str(missing_key)]
                )
            self.assertEqual(code, 2)
            self.assertIn("Credentials file not found", stderr.getvalue())

            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [str(path), "--file-id", "abc", "--credentials", str(bad)]
                )
            self.assertEqual(code, 1)
            self.assertIn("not valid JSON", stderr.getvalue())

    def test_missing_adc_mentions_drive_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "BRIEF.md"
            path.write_text("# ok\n", encoding="utf-8")
            stderr = io.StringIO()
            with patch(
                "google.auth.default",
                side_effect=DefaultCredentialsError("no creds"),
            ):
                with redirect_stderr(stderr):
                    code = main([str(path), "--file-id", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("Drive scope", stderr.getvalue())
        self.assertNotIn("Sheets scope", stderr.getvalue())


class UpdateTests(unittest.TestCase):
    def _creds(self):
        creds = MagicMock()
        creds.service_account_email = "bot@example.iam.gserviceaccount.com"
        return creds

    def _local(self, directory: str, name: str, text: str, mime: str | None = None) -> LocalFile:
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return LocalFile(
            path=path,
            size=path.stat().st_size,
            mime_type=mime or guess_mime(path),
        )

    def test_one_media_update_keeps_the_file_id(self) -> None:
        drive = MagicMock()
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "file123",
            "webViewLink": "https://drive.google.com/file/d/file123/view",
        }
        with tempfile.TemporaryDirectory() as tmp:
            local = self._local(tmp, "BRIEF.md", "# Brief\n")
            payload = local.path.read_bytes()
            result = update_file(
                drive,
                self._creds(),
                local,
                file_id="file123",
                name=None,
            )
            media = drive.files.return_value.update.call_args.kwargs["media_body"]
            self.assertEqual(media._filename, str(local.path))
            self.assertEqual(media.size(), len(payload))
            self.assertEqual(payload, b"# Brief\n")

        files = drive.files.return_value
        files.update.assert_called_once()
        files.create.assert_not_called()
        files.delete.assert_not_called()
        files.copy.assert_not_called()
        files.emptyTrash.assert_not_called()

        sent = files.update.call_args.kwargs
        self.assertEqual(sent["fileId"], "file123")
        self.assertEqual(sent["body"], {})
        self.assertNotIn("trashed", sent["body"])
        self.assertNotIn("addParents", sent)
        self.assertNotIn("removeParents", sent)
        self.assertEqual(sent["fields"], "id,webViewLink")
        self.assertTrue(sent["supportsAllDrives"])
        self.assertIsInstance(sent["media_body"], MediaFileUpload)
        self.assertEqual(sent["media_body"].mimetype(), "text/markdown")
        self.assertFalse(sent["media_body"].resumable())
        self.assertEqual(result.file_id, "file123")
        self.assertEqual(
            result.web_view_url,
            "https://drive.google.com/file/d/file123/view",
        )

    def test_name_is_metadata_on_the_same_update(self) -> None:
        drive = MagicMock()
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "file123",
        }
        with tempfile.TemporaryDirectory() as tmp:
            local = self._local(tmp, "notes.txt", "hello")
            result = update_file(
                drive,
                self._creds(),
                local,
                file_id="file123",
                name="Weekly brief",
            )
        sent = drive.files.return_value.update.call_args.kwargs
        self.assertEqual(sent["body"], {"name": "Weekly brief"})
        self.assertEqual(sent["media_body"].mimetype(), "text/plain")
        self.assertEqual(
            result.web_view_url,
            "https://drive.google.com/file/d/file123/view",
        )
        drive.files.return_value.create.assert_not_called()

    def test_large_file_uses_resumable_upload(self) -> None:
        drive = MagicMock()
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "file123",
            "webViewLink": "https://drive.google.com/file/d/file123/view?usp=drivesdk",
        }
        with tempfile.TemporaryDirectory() as tmp:
            local = self._local(tmp, "brief.md", "# x\n")
            local = LocalFile(
                path=local.path,
                size=RESUMABLE_THRESHOLD_BYTES,
                mime_type="text/markdown",
            )
            result = update_file(
                drive,
                self._creds(),
                local,
                file_id="file123",
                name=None,
            )
        media = drive.files.return_value.update.call_args.kwargs["media_body"]
        self.assertTrue(media.resumable())
        self.assertEqual(
            result.web_view_url,
            "https://drive.google.com/file/d/file123/view?usp=drivesdk",
        )

    def test_mismatched_id_is_an_error(self) -> None:
        drive = MagicMock()
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "other-file",
            "webViewLink": "https://drive.google.com/file/d/other-file/view",
        }
        with tempfile.TemporaryDirectory() as tmp:
            local = self._local(tmp, "BRIEF.md", "# x\n")
            with self.assertRaises(DriveUpdateError) as ctx:
                update_file(
                    drive,
                    self._creds(),
                    local,
                    file_id="file123",
                    name=None,
                )
        self.assertIn("other-file", str(ctx.exception))
        self.assertIn("file123", str(ctx.exception))
        self.assertEqual(ctx.exception.exit_code, 1)
        drive.files.return_value.update.assert_called_once()
        drive.files.return_value.create.assert_not_called()

    def test_permission_errors_hint_to_share_as_editor(self) -> None:
        hint_403 = access_hint(403, "bot@example.iam.gserviceaccount.com")
        self.assertIn("bot@example.iam.gserviceaccount.com", hint_403)
        self.assertIn("Editor", hint_403)
        hint_404 = access_hint(404, "bot@example.iam.gserviceaccount.com")
        self.assertIn("file id", hint_404)
        self.assertIn("Editor", hint_404)
        self.assertIn("Editor", access_hint(404, None))
        self.assertEqual(access_hint(500, "bot@example.com"), "")

        for status in (403, 404):
            drive = MagicMock()
            drive.files.return_value.update.return_value.execute.side_effect = (
                _http_error(status, "permission")
            )
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "data.json"
                path.write_text("{}", encoding="utf-8")
                stderr = io.StringIO()
                with patch(
                    "drive_update_file.load_credentials",
                    return_value=self._creds(),
                ) as load, patch(
                    "drive_update_file.build_drive",
                    return_value=drive,
                ):
                    with redirect_stderr(stderr):
                        code = main([str(path), "--file-id", "file123"])
                load.assert_called_once_with(None, [DRIVE_SCOPE])
            self.assertEqual(code, 1)
            message = stderr.getvalue()
            self.assertTrue(message.startswith("error:"))
            self.assertIn("file123", message)
            self.assertIn(f"({status})", message)
            self.assertIn("bot@example.iam.gserviceaccount.com", message)
            self.assertIn("Editor", message)
            drive.files.return_value.update.assert_called_once()
            drive.files.return_value.create.assert_not_called()
            self.assertEqual(
                drive.files.return_value.update.call_args.kwargs["media_body"].mimetype(),
                "application/json",
            )

    def test_main_prints_link_and_id(self) -> None:
        drive = MagicMock()
        drive.files.return_value.update.return_value.execute.return_value = {
            "id": "file123",
            "webViewLink": "https://drive.google.com/file/d/file123/view",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.csv"
            path.write_text("a,b\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch(
                "drive_update_file.load_credentials",
                return_value=self._creds(),
            ), patch("drive_update_file.build_drive", return_value=drive):
                with redirect_stdout(stdout):
                    code = main([str(path), "--file-id", "file123"])
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "https://drive.google.com/file/d/file123/view",
                "id: file123",
            ],
        )
        sent = drive.files.return_value.update.call_args.kwargs
        self.assertEqual(sent["fileId"], "file123")
        self.assertEqual(sent["media_body"].mimetype(), "text/csv")
        drive.files.return_value.create.assert_not_called()

    def test_source_only_updates(self) -> None:
        source = Path(__file__).resolve().parents[1] / "drive_update_file.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("files().update", text)
        self.assertNotIn("files().create", text)
        self.assertNotIn("files().delete", text)
        self.assertNotIn("files().emptyTrash", text)
        self.assertNotIn("trashed", text)


if __name__ == "__main__":
    unittest.main()
