#!/usr/bin/env python3
"""Fetch Wikipedia thumbnail photos for all voices in voices.json."""

import json
import time
import urllib.request
import urllib.parse
import urllib.error
import re
import ssl

VOICES_PATH = "data/voices.json"

# Manual Wikipedia article name overrides for tricky cases
WIKI_OVERRIDES = {
    "jordan-peterson": "Jordan_Peterson",
    "full-send": "Nelk",
    "young-turks": "The_Young_Turks",
    "breakfast-club": "The_Breakfast_Club_(radio_show)",
    "phil-mcgraw": "Phil_McGraw",
    "hasanabi": "Hasan_Piker",
    "destiny": "Destiny_(streamer)",
    "breaking-points": "Breaking_Points_with_Krystal_and_Saagar",
    "aoc": "Alexandria_Ocasio-Cortez",
    "pod-save-america": "Pod_Save_America",
    "charlamagne-tha-god": "Charlamagne_tha_God",
    "asmongold": "Asmongold",
    "contrapoints": "ContraPoints",
    "sneako": "Sneako",
    "zuby": "Zuby_(rapper)",
    "jidion": "JiDion",
    "hbomberguy": "Hbomberguy",
    "kai-cenat": "Kai_Cenat",
    "stephen-a-smith": "Stephen_A._Smith",
    "patrick-bet-david": "Patrick_Bet-David",
    "brian-tyler-cohen": "Brian_Tyler_Cohen",
    "kyle-kulinski": "Kyle_Kulinski",
    "benny-johnson": "Benny_Johnson_(media_personality)",
    "ana-kasparian": "Ana_Kasparian",
    "danny-gonzalez": "Danny_Gonzalez_(YouTuber)",
    "drew-gooden": "Drew_Gooden_(YouTuber)",
    "philip-defranco": "Philip_DeFranco",
    "sage-steele": "Sage_Steele",
    "briahna-joy-gray": "Briahna_Joy_Gray",
}

# Create an SSL context that doesn't verify (for corporate/dev environments)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def name_to_wiki(voice):
    """Convert voice name to Wikipedia article title."""
    vid = voice["id"]
    if vid in WIKI_OVERRIDES:
        return WIKI_OVERRIDES[vid]
    # Use the display name, replace spaces with underscores
    name = voice["name"]
    # Strip parenthetical qualifiers like "(Dr. Phil)"
    name = re.sub(r'\s*\(.*?\)', '', name).strip()
    return name.replace(" ", "_")


def get_wiki_thumbnail(article_name, width=300):
    """Query Wikipedia REST API for page summary thumbnail."""
    encoded = urllib.parse.quote(article_name, safe='')
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"

    req = urllib.request.Request(url, headers={
        "User-Agent": "NewsreelBot/1.0 (jack@newsreel.co)"
    })

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)

    thumb = data.get("thumbnail", {})
    src = thumb.get("source")
    if not src:
        return None, "no thumbnail"

    # Adjust width to 300px
    # Wikipedia thumbnail URLs look like: .../220px-Foo.jpg
    src = re.sub(r'/\d+px-', f'/{width}px-', src)

    return src, None


def make_fallback(name):
    """Generate a ui-avatars fallback URL."""
    encoded = urllib.parse.quote(name)
    return f"https://ui-avatars.com/api/?name={encoded}&size=300&background=1a1a2e&color=fff"


def main():
    with open(VOICES_PATH) as f:
        voices = json.load(f)

    success = 0
    failed = 0
    already_wiki = 0
    fallback_used = 0

    for i, voice in enumerate(voices):
        vid = voice["id"]
        name = voice["name"]

        article = name_to_wiki(voice)
        photo_url, err = get_wiki_thumbnail(article)

        if photo_url:
            voice["photo"] = photo_url
            success += 1
            print(f"[OK]   {name:40s} -> {article}")
        else:
            # Try alternate: just the raw name with underscores
            alt = name.replace(" ", "_")
            if alt != article:
                photo_url2, err2 = get_wiki_thumbnail(alt)
                if photo_url2:
                    voice["photo"] = photo_url2
                    success += 1
                    print(f"[OK2]  {name:40s} -> {alt}")
                    time.sleep(0.3)
                    continue

            # Use fallback
            voice["photo"] = make_fallback(name)
            fallback_used += 1
            failed += 1
            print(f"[FAIL] {name:40s} ({err})")

        # Rate limit
        time.sleep(0.3)

    with open(VOICES_PATH, "w") as f:
        json.dump(voices, f, indent=2)
        f.write("\n")

    print(f"\n--- Results ---")
    print(f"Total voices:     {len(voices)}")
    print(f"Photos found:     {success}")
    print(f"Fallback used:    {fallback_used}")
    print(f"voices.json updated.")


if __name__ == "__main__":
    main()
