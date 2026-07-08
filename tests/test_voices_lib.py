#!/usr/bin/env python3
"""Tests for scripts/voices_lib.py — the voice-profile access + validation layer.

Run from the repo root with the stdlib test runner (no dependencies):

    python -m unittest discover -s tests -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Make scripts/ importable regardless of the current working directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import voices_lib as vl  # noqa: E402


def _voice(**over):
    """A minimal valid voice profile, with overrides."""
    base = {"id": "jane-doe", "name": "Jane Doe", "photo": "/photos/jane-doe.jpg", "lens": "Analyst"}
    base.update(over)
    return base


class LoadVoicesTests(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def test_loads_valid_array(self):
        path = self._write(json.dumps([_voice()]))
        voices = vl.load_voices(path)
        self.assertEqual(len(voices), 1)
        self.assertEqual(voices[0]["name"], "Jane Doe")

    def test_missing_file_raises(self):
        with self.assertRaises(vl.VoicesError):
            vl.load_voices(ROOT / "data" / "does-not-exist.json")

    def test_invalid_json_raises(self):
        path = self._write("{not json")
        with self.assertRaises(vl.VoicesError):
            vl.load_voices(path)

    def test_non_array_raises(self):
        path = self._write(json.dumps({"voices": []}))
        with self.assertRaises(vl.VoicesError):
            vl.load_voices(path)


class IndexByIdTests(unittest.TestCase):
    def test_indexes_and_last_wins(self):
        a = _voice(id="x", name="First")
        b = _voice(id="x", name="Second")
        idx = vl.index_by_id([a, b])
        self.assertEqual(idx["x"]["name"], "Second")

    def test_skips_malformed_entries(self):
        idx = vl.index_by_id([_voice(id="ok"), "garbage", {"no": "id"}])
        self.assertEqual(list(idx), ["ok"])


class VoicePhotoTests(unittest.TestCase):
    def test_returns_real_photo(self):
        self.assertEqual(vl.voice_photo({"photo": "/photos/x.jpg"}, "X"), "/photos/x.jpg")

    def test_falls_back_when_missing(self):
        url = vl.voice_photo({}, "Jane Doe")
        self.assertIn("ui-avatars.com", url)
        self.assertIn("Jane%20Doe", url)

    def test_fallback_url_is_byte_identical(self):
        # Guards the exact ui-avatars styling contract downstream renders rely on.
        self.assertEqual(
            vl.voice_photo({}, "Jane Doe"),
            "https://ui-avatars.com/api/?name=Jane%20Doe"
            "&background=252528&color=a1a1aa&size=96",
        )

    def test_regenerates_over_existing_ui_avatar(self):
        url = vl.voice_photo({"photo": "https://ui-avatars.com/api/?name=old"}, "New Name")
        self.assertIn("New%20Name", url)

    def test_handles_none_meta(self):
        self.assertIn("ui-avatars.com", vl.voice_photo(None, "Someone"))


class AbsolutePhotoUrlTests(unittest.TestCase):
    def test_prefixes_relative_path(self):
        self.assertEqual(
            vl.absolute_photo_url("/photos/x.jpg"),
            "https://newsreel-perspectives.onrender.com/photos/x.jpg",
        )

    def test_leaves_absolute_url_untouched(self):
        self.assertEqual(vl.absolute_photo_url("https://cdn/x.jpg"), "https://cdn/x.jpg")

    def test_only_photos_paths_are_prefixed(self):
        # Non-/photos/ paths and protocol-relative URLs must be left alone,
        # not blindly host-prefixed into a broken URL.
        self.assertEqual(vl.absolute_photo_url("/avatars/x.jpg"), "/avatars/x.jpg")
        self.assertEqual(vl.absolute_photo_url("//cdn/x.jpg"), "//cdn/x.jpg")

    def test_empty_is_safe(self):
        self.assertEqual(vl.absolute_photo_url(""), "")

    def test_custom_host(self):
        self.assertEqual(vl.absolute_photo_url("/photos/x.jpg", host="https://h"), "https://h/photos/x.jpg")


class VoiceLensTests(unittest.TestCase):
    def test_returns_lens(self):
        self.assertEqual(vl.voice_lens({"lens": "Economist"}), "Economist")

    def test_default_when_empty(self):
        self.assertEqual(vl.voice_lens({"lens": ""}), "commentator")
        self.assertEqual(vl.voice_lens({}), "commentator")
        self.assertEqual(vl.voice_lens(None), "commentator")


class ValidateVoicesTests(unittest.TestCase):
    def _levels(self, issues, field=None):
        return [i for i in issues if field is None or i.field == field]

    def test_clean_data_has_no_issues(self):
        self.assertEqual(vl.validate_voices([_voice()]), [])

    def test_missing_required_field_is_error(self):
        issues = vl.validate_voices([_voice(lens="")])
        self.assertTrue(any(i.level == "error" and i.field == "lens" for i in issues))

    def test_duplicate_id_is_error(self):
        issues = vl.validate_voices([_voice(id="dup"), _voice(id="dup", name="Other")])
        self.assertTrue(any(i.level == "error" and i.field == "id" for i in issues))

    def test_duplicate_name_is_warning(self):
        issues = vl.validate_voices([_voice(id="a", name="Same"), _voice(id="b", name="Same")])
        self.assertTrue(any(i.level == "warning" and i.field == "name" for i in issues))

    def test_odd_photo_path_is_warning(self):
        issues = vl.validate_voices([_voice(photo="photos/no-leading-slash.jpg")])
        self.assertTrue(any(i.level == "warning" and i.field == "photo" for i in issues))

    def test_wrong_container_type_is_warning(self):
        issues = vl.validate_voices([_voice(tags="not-a-list")])
        self.assertTrue(any(i.level == "warning" and i.field == "tags" for i in issues))

    def test_non_string_id_is_error(self):
        issues = vl.validate_voices([_voice(id=123)])
        self.assertTrue(any(i.level == "error" and i.field == "id" for i in issues))

    def test_non_dict_entry_is_error(self):
        issues = vl.validate_voices([_voice(), "garbage"])
        self.assertTrue(any(i.level == "error" for i in issues))

    def test_non_list_root_is_error(self):
        issues = vl.validate_voices({"not": "a list"})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].level, "error")


class CliMainTests(unittest.TestCase):
    """The CLI is the module's advertised job — its exit codes are a contract."""

    def _write(self, payload):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.write(payload if isinstance(payload, str) else json.dumps(payload))
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return tmp.name

    def _run(self, args):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = vl.main(args)
        return code, buf.getvalue()

    def test_clean_data_exits_zero(self):
        path = self._write([_voice()])
        code, out = self._run([path])
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_errors_exit_one(self):
        path = self._write([_voice(id="dup"), _voice(id="dup", name="Other")])
        code, _ = self._run([path])
        self.assertEqual(code, 1)

    def test_unreadable_file_exits_two(self):
        code, _ = self._run([str(ROOT / "data" / "nope.json")])
        self.assertEqual(code, 2)

    def test_strict_promotes_warnings_to_failure(self):
        path = self._write([_voice(id="a", name="Same"), _voice(id="b", name="Same")])
        self.assertEqual(self._run([path])[0], 0)            # warning only → ok
        self.assertEqual(self._run([path, "--strict"])[0], 1)  # strict → fail

    def test_json_report_is_machine_readable(self):
        path = self._write([_voice(id="dup"), _voice(id="dup", name="Other")])
        code, out = self._run([path, "--json"])
        report = json.loads(out)
        self.assertFalse(report["ok"])
        self.assertEqual(report["total"], 2)
        self.assertTrue(report["errors"])

    def test_json_report_on_load_failure(self):
        code, out = self._run([str(ROOT / "data" / "nope.json"), "--json"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])


class RealDataContractTest(unittest.TestCase):
    """The shipped data/voices.json must satisfy its own contract (no errors)."""

    def test_production_voices_have_no_errors(self):
        voices = vl.load_voices()
        errors = [i for i in vl.validate_voices(voices) if i.level == "error"]
        self.assertEqual(errors, [], msg=f"voices.json has contract errors: {errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
