#!/usr/bin/env python3
"""
Put an API key into .env safely.

  python3 scripts/set_key.py TYPESAFE_API_KEY

Input is hidden, so the key never appears on screen, in your shell history, or
in a chat window. The value is validated before anything is written, because a
bad clipboard paste once put 23KB of a document into this file and broke every
script that reads it.
"""
import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    name = sys.argv[1].strip()
    if not (name.isidentifier() and name.isupper()):
        print(f"'{name}' is not a valid variable name (UPPER_SNAKE_CASE).")
        return 2

    value = getpass.getpass(f"Paste {name} (input hidden, then press Enter): ").strip()

    problems = []
    if not value:
        problems.append("it is empty")
    if " " in value or "\t" in value:
        problems.append("it contains a space, so it is probably not a key")
    if "\n" in value:
        problems.append("it contains a line break")
    if len(value) > 300:
        problems.append(f"it is {len(value)} characters, which is too long for an API key")
    if len(value) < 8:
        problems.append(f"it is only {len(value)} characters, which is too short")
    if not re.fullmatch(r"[\x21-\x7e]+", value):
        problems.append("it contains non-printable or non-ASCII characters")
    if value.lower().startswith("bearer "):
        problems.append("it starts with 'Bearer '; paste the key alone")
    if problems:
        print("\nNot written. What you pasted looks wrong:")
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

    print(f"\nWrote {name} to {ENV} ({len(value)} characters).")
    print("Keys now in the file:")
    for ln in ENV.read_text().splitlines():
        k, _, v = ln.partition("=")
        print(f"  {k} ({len(v)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
