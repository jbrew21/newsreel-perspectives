#!/usr/bin/env python3
"""
Put an API key into .env safely. Three ways in, so one of them always works.

  python3 scripts/set_key.py TYPESAFE_API_KEY              hidden prompt
  python3 scripts/set_key.py TYPESAFE_API_KEY --visible    shown prompt
  python3 scripts/set_key.py TYPESAFE_API_KEY --from-file ~/key.txt
  python3 scripts/set_key.py TYPESAFE_API_KEY --clipboard  read the clipboard

The value is validated before anything is written, because a bad clipboard
paste once put 23KB of a document into this file and broke every script that
reads it (Sep 18 2026).
"""
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def read_value(name, argv):
    if "--from-file" in argv:
        i = argv.index("--from-file")
        if i + 1 >= len(argv):
            print("--from-file needs a path")
            sys.exit(2)
        path = Path(argv[i + 1]).expanduser()
        if not path.exists():
            print(f"No such file: {path}")
            sys.exit(2)
        return path.read_text(errors="replace").strip(), path

    if "--clipboard" in argv:
        try:
            out = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            return out.stdout.strip(), None
        except Exception as e:
            print(f"Could not read the clipboard: {e}")
            sys.exit(2)

    if "--visible" in argv or not sys.stdin.isatty():
        if not sys.stdin.isatty():
            print("(no interactive terminal detected, so the prompt is visible)")
        return input(f"Paste {name} and press Enter: ").strip(), None

    print("Your terminal will show NOTHING while you paste. That is normal.")
    print("Paste the key, then press Enter.")
    try:
        return getpass.getpass(f"{name}: ").strip(), None
    except Exception:
        print("\nHidden input is unavailable here. Falling back to a visible prompt.")
        return input(f"Paste {name} and press Enter: ").strip(), None


def validate(value):
    problems = []
    if not value:
        problems.append("nothing was entered")
        return problems
    if " " in value or "\t" in value:
        problems.append("it contains a space, so it is probably not a key")
    if len(value) > 300:
        problems.append(f"it is {len(value)} characters, too long for an API key")
    if len(value) < 8:
        problems.append(f"it is only {len(value)} characters, too short")
    if not re.fullmatch(r"[\x21-\x7e]+", value):
        problems.append("it contains non-printable or non-ASCII characters")
    if value.lower().startswith("bearer "):
        problems.append("it starts with 'Bearer '; paste the key alone")
    return problems


def main():
    argv = sys.argv[1:]
    names = [a for a in argv if not a.startswith("-")]
    if not names:
        print(__doc__.strip())
        return 2
    name = names[0]
    if not (name.isidentifier() and name.isupper()):
        print(f"'{name}' is not a valid variable name (UPPER_SNAKE_CASE).")
        return 2

    value, src_file = read_value(name, argv)
    problems = validate(value)
    if problems:
        print("\nNot written. What you entered looks wrong:")
        for p in problems:
            print(f"  - {p}")
        print("\nCopy the key on its own from the provider's console and try again.")
        return 1

    lines = []
    if ENV.exists():
        text = ENV.read_bytes().decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines()
                 if ln.strip() and not ln.strip().startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)

    print(f"\nWrote {name} ({len(value)} characters) to {ENV}")
    print("Keys now in the file:")
    for ln in ENV.read_text().splitlines():
        k, _, v = ln.partition("=")
        print(f"  {k} ({len(v)} chars)")

    if src_file:
        try:
            src_file.unlink()
            print(f"\nDeleted {src_file} so the key is not left lying around.")
        except OSError:
            print(f"\nCould not delete {src_file}. Remove it yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
