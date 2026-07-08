#!/usr/bin/env python3
"""
Newsreel Perspectives — Voice Profile Library

Canonical, single-source-of-truth access layer for voice profiles
(``data/voices.json``). Every script that reads voice metadata should go
through here so that path resolution, the photo/lens fallback rules, and the
data contract stay identical everywhere instead of being copy-pasted.

It also provides schema/integrity validation and a CLI:

    python scripts/voices_lib.py            # validate data/voices.json
    python scripts/voices_lib.py --strict   # treat warnings as errors too
    python scripts/voices_lib.py --json     # machine-readable report

Exit codes: 0 = clean, 1 = errors found (or warnings under --strict),
2 = the file could not be loaded at all.
"""

import argparse
import json
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"

# Host used to turn a repo-relative "/photos/foo.jpg" path into an absolute URL
# (e.g. for emails and other off-site consumers). Centralized here so it is not
# re-hardcoded across scripts.
PHOTO_HOST = "https://newsreel-perspectives.onrender.com"

# Fields every voice profile must carry a non-empty value for.
REQUIRED_FIELDS = ("id", "name", "photo", "lens")

# ui-avatars styling for the generated fallback avatar.
_AVATAR_BG = "252528"
_AVATAR_FG = "a1a1aa"
_AVATAR_SIZE = 96


class VoicesError(Exception):
    """Raised when voices.json cannot be read or is structurally invalid."""


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_voices(path=VOICES_PATH):
    """Load and return the list of voice profiles.

    Raises ``VoicesError`` (never silently returns empty) when the file is
    missing, is not valid JSON, or is not a JSON array. Callers that want to
    degrade gracefully can catch ``VoicesError`` explicitly.
    """
    path = Path(path)
    try:
        raw = path.read_text()
    except OSError as exc:
        raise VoicesError(f"cannot read voices file at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VoicesError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise VoicesError(
            f"{path} must contain a JSON array of voices, got {type(data).__name__}"
        )
    return data


def index_by_id(voices):
    """Return a ``{id: voice}`` map. On duplicate ids, the last entry wins,
    matching the historical ``{v['id']: v for v in voices}`` behaviour."""
    return {v["id"]: v for v in voices if isinstance(v, dict) and "id" in v}


# --------------------------------------------------------------------------- #
# Field accessors (the single source of truth for photo/lens rules)
# --------------------------------------------------------------------------- #

def voice_photo(meta, voice_name):
    """Return a usable photo URL for a voice.

    Uses the profile's real photo when present, otherwise generates a
    deterministic ui-avatars fallback from the name. An existing ui-avatars
    URL is treated as "no real photo" so it gets regenerated consistently.
    """
    photo = meta.get("photo", "") if meta else ""
    if photo and "ui-avatars.com" not in photo:
        return photo
    encoded = urllib.parse.quote(voice_name or "")
    return (
        f"https://ui-avatars.com/api/?name={encoded}"
        f"&background={_AVATAR_BG}&color={_AVATAR_FG}&size={_AVATAR_SIZE}"
    )


def absolute_photo_url(photo, host=PHOTO_HOST):
    """Absolutize a repo-relative ``/photos/...`` path against ``host``.

    Only ``/photos/...`` paths are prefixed (mirroring the contract enforced by
    :func:`validate_voices`). Absolute URLs, protocol-relative URLs, empty
    values, and any other path are returned unchanged, so this is safe to call
    on any ``photo`` value.
    """
    if photo and photo.startswith("/photos/"):
        return f"{host}{photo}"
    return photo


def voice_lens(meta, default="commentator"):
    """Return the voice's lens (its one-line professional identity), or
    ``default`` when it is missing/empty."""
    if not meta:
        return default
    return meta.get("lens") or default


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

class Issue(NamedTuple):
    level: str          # "error" | "warning"
    voice_id: str       # best-effort id/name, or "<index N>"
    field: str
    message: str


def validate_voices(voices):
    """Return a list of :class:`Issue` describing contract violations.

    Errors are things that will break consumers (missing required fields,
    duplicate ids, non-dict entries). Warnings are data-quality smells
    (duplicate names, odd photo paths, wrong container types) that degrade the
    experience but do not crash. An empty list means the data is clean.
    """
    issues = []

    if not isinstance(voices, list):
        return [Issue("error", "<root>", "", "top-level value must be a list")]

    ids = []
    names = []

    for i, v in enumerate(voices):
        if not isinstance(v, dict):
            issues.append(
                Issue("error", f"<index {i}>", "", f"entry must be an object, got {type(v).__name__}")
            )
            continue

        label = v.get("id") or v.get("name") or f"<index {i}>"

        for field in REQUIRED_FIELDS:
            value = v.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(Issue("error", label, field, f"missing or empty required field '{field}'"))

        for field in ("id", "name"):
            if field in v and v[field] is not None and not isinstance(v[field], str):
                issues.append(Issue("error", label, field,
                                    f"'{field}' must be a string, got {type(v[field]).__name__}"))

        if isinstance(v.get("id"), str) and v["id"]:
            ids.append(v["id"])
        if isinstance(v.get("name"), str) and v["name"]:
            names.append(v["name"])

        photo = v.get("photo")
        if isinstance(photo, str) and photo:
            if not (photo.startswith("/photos/") or photo.startswith("http://")
                    or photo.startswith("https://")):
                issues.append(Issue("warning", label, "photo",
                                    f"photo path is neither '/photos/...' nor an absolute URL: {photo!r}"))

        for container, typ in (("handles", dict), ("feeds", (dict, list)), ("tags", list)):
            if container in v and not isinstance(v[container], typ):
                want = " or ".join(t.__name__ for t in (typ if isinstance(typ, tuple) else (typ,)))
                issues.append(Issue("warning", label, container,
                                    f"'{container}' should be {want}, got {type(v[container]).__name__}"))

    for dup, count in Counter(ids).items():
        if count > 1:
            issues.append(Issue("error", dup, "id", f"duplicate id used by {count} voices"))
    for dup, count in Counter(names).items():
        if count > 1:
            issues.append(Issue("warning", dup, "name", f"duplicate name used by {count} voices"))

    return issues


def format_report(issues, total):
    """Render a human-readable validation report."""
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    lines = [f"Validated {total} voice profiles: {len(errors)} error(s), {len(warnings)} warning(s)."]
    for issue in errors + warnings:
        badge = "ERROR  " if issue.level == "error" else "warning"
        where = f"{issue.voice_id}" + (f".{issue.field}" if issue.field else "")
        lines.append(f"  [{badge}] {where}: {issue.message}")
    if not issues:
        lines.append("  ✓ clean")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the voice profile database.")
    parser.add_argument("path", nargs="?", default=str(VOICES_PATH),
                        help="path to voices.json (default: data/voices.json)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit a machine-readable JSON report")
    args = parser.parse_args(argv)

    try:
        voices = load_voices(args.path)
    except VoicesError as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    issues = validate_voices(voices)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    if args.as_json:
        print(json.dumps({
            "ok": not errors and not (args.strict and warnings),
            "total": len(voices),
            "errors": [i._asdict() for i in errors],
            "warnings": [i._asdict() for i in warnings],
        }, indent=2))
    else:
        print(format_report(issues, len(voices)))

    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
