#!/usr/bin/env python3
"""Generate permanent position tags for all voices using Claude."""

import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Load env
for env_path in [ROOT.parent / "newsletter" / ".env", ROOT / ".env"]:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                k, _, v = line.partition('=')
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

api_key = os.environ.get('ANTHROPIC_API_KEY', '')
voices = json.loads((ROOT / "data" / "voices.json").read_text())

# Build voice list
voice_lines = []
for v in voices:
    voice_lines.append(f'- {v["name"]} (id: {v["id"]}): {v.get("lens", "unknown")}')

voices_block = '\n'.join(voice_lines)

prompt = (
    "Here are 78 public commentators with their bios. For each one, assign exactly "
    "2-3 short permanent tags (1-3 words each) that describe their general ideological "
    "position and focus areas.\n\n"
    "These tags should be:\n"
    "- STABLE: they describe the person's ongoing positions, not reactions to any one story\n"
    "- SPECIFIC: not just 'conservative' or 'liberal' but things like 'libertarian right', "
    "'anti-war', 'pro-free speech', 'populist', 'progressive left'\n"
    "- HONEST: accurately reflect where this person sits\n\n"
    "Examples of good tags: 'populist right', 'anti-establishment', 'pro-Israel', "
    "'progressive left', 'libertarian', 'anti-war', 'free speech advocate', "
    "'democratic socialist', 'neoconservative', 'centrist', 'culture warrior'\n\n"
    f"{voices_block}\n\n"
    "Return a JSON object mapping voice ID to an array of 2-3 tag strings.\n"
    'Example: {"joe-rogan": ["libertarian-leaning", "anti-establishment", "free speech"], '
    '"ben-shapiro": ["conservative", "pro-Israel", "constitutionalist"]}'
)

req = urllib.request.Request(
    'https://api.anthropic.com/v1/messages',
    data=json.dumps({
        'model': 'claude-sonnet-4-20250514',
        'max_tokens': 4096,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode(),
    headers={
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    },
)

print("Generating tags for 78 voices...")
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.loads(resp.read().decode())

result_text = data.get('content', [{}])[0].get('text', '')
json_match = re.search(r'\{[\s\S]*\}', result_text)
if json_match:
    tags = json.loads(json_match.group())

    for v in voices:
        v['tags'] = tags.get(v['id'], [])

    (ROOT / "data" / "voices.json").write_text(json.dumps(voices, indent=2))

    tagged = sum(1 for v in voices if v.get('tags'))
    print(f'Tagged {tagged}/{len(voices)} voices\n')
    for v in voices[:15]:
        print(f'  {v["name"]}: {v.get("tags", [])}')
else:
    print("Failed to parse response")
