#!/usr/bin/env python3
"""
One-time X/Twitter login for twikit.
Saves cookies so future searches don't need to re-auth.

Usage:
  python scripts/x_login.py
"""

import asyncio
from pathlib import Path

ROOT = Path(__file__).parent.parent

async def main():
    from twikit import Client

    client = Client('en-US')

    print("\n  X/Twitter Login for Newsreel Perspectives")
    print("  ──────────────────────────────────────────")
    print("  This saves cookies so future searches work automatically.\n")

    username = input("  X username or email: ").strip()
    email = input("  X email (if different): ").strip() or username
    password = input("  X password: ").strip()

    print("\n  Logging in...")
    try:
        await client.login(auth_info_1=username, auth_info_2=email, password=password)
        cookies_path = ROOT / ".x_cookies.json"
        client.save_cookies(str(cookies_path))
        print(f"  ✓ Logged in! Cookies saved to {cookies_path}")
        print("  You can now run: python scripts/search.py \"topic\"")
    except Exception as e:
        print(f"  ✗ Login failed: {e}")
        print("  If you have 2FA, you may need to use an app password.")

if __name__ == '__main__':
    asyncio.run(main())
