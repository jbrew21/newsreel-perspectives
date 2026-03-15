#!/usr/bin/env python3
"""
Perspective Profiles — maps top 50 poll users against 257 tracked voices.

Reads pre-fetched data from /tmp (Supabase exports) and data/voices.json,
computes topic stances, matches to voices, and generates beautiful HTML profiles.

Usage:
    python3 scripts/perspective_profiles.py
"""

import json
import os
import re
import html as html_mod
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES_PATH = ROOT / "data" / "voices.json"
PROFILES_DIR = ROOT / "data" / "profiles"

# ─── Load data ────────────────────────────────────────────────────────────────

def load_users():
    """Top 50 users from data/mirror-users.json (gitignored, contains PII)."""
    users_path = ROOT / "data" / "mirror-users.json"
    if not users_path.exists():
        raise FileNotFoundError("data/mirror-users.json not found. Export from Supabase first.")
    return json.loads(users_path.read_text())

def _REMOVED_hardcoded_users():
    """REMOVED: User data moved to gitignored file. Original had:"""
    return []  # Data removed — was exposing user PII. Load from data/mirror-users.json instead.


def load_poll_responses():
    """Load pre-fetched poll responses from /tmp."""
    all_responses = []
    for batch in ["/tmp/poll_responses_batch1.json", "/tmp/poll_responses_batch2.json"]:
        with open(batch) as f:
            all_responses.extend(json.load(f))
    return all_responses


def load_question_averages():
    """Load the overall averages for each question."""
    with open("/tmp/question_averages.json") as f:
        data = json.load(f)
    return {row["question"]: {"avg": float(row["avg_response"]), "count": int(row["response_count"])} for row in data}


def load_voices():
    with open(VOICES_PATH) as f:
        return json.load(f)


# ─── Topic classification ────────────────────────────────────────────────────

TOPIC_RULES = [
    ("foreign-policy", ["iran", "military", "troops", "war", "nato", "missile", "strike", "pentagon", "defense", "army", "navy"]),
    ("immigration", ["immigration", "ice ", "border", "deport", "migrant", "undocumented", "asylum", "refugee"]),
    ("climate", ["climate", "environment", "green", "carbon", "fossil", "emissions", "wildfire", "renewable"]),
    ("economy", ["economy", "tariff", "trade", "inflation", "jobs", "wage", "recession", "gdp", "tax", "budget", "deficit", "spending"]),
    ("technology", [" ai ", "ai-", "artificial intelligence", "tech", "tiktok", "social media", "algorithm", "data privacy", "surveillance"]),
    ("guns", ["gun", "second amendment", "firearm", "shooting", "nra", "mass shooting"]),
    ("israel-palestine", ["israel", "palestin", "gaza", "hamas", "netanyahu", "west bank", "ceasefire"]),
    ("education", ["education", "student", "school", "college", "university", "campus", "tuition"]),
    ("healthcare", ["healthcare", "drug", "medical", "vaccine", "health insurance", "medicare", "medicaid", "fda"]),
    ("free-speech", ["free speech", "censorship", "first amendment", "content moderation", "misinformation"]),
    ("civil-rights", ["civil rights", "discrimination", "dei", "diversity", "equity", "inclusion", "lgbtq", "race", "racial"]),
    ("russia-ukraine", ["russia", "ukraine", "putin", "kyiv", "kremlin", "sanction"]),
]


def classify_topic(question: str) -> str:
    q_lower = question.lower()
    for topic, keywords in TOPIC_RULES:
        for kw in keywords:
            if kw in q_lower:
                return topic
    return "general"


TOPIC_LABELS = {
    "foreign-policy": "Foreign Policy",
    "immigration": "Immigration",
    "climate": "Climate & Environment",
    "economy": "Economy & Trade",
    "technology": "Technology & AI",
    "guns": "Guns & Safety",
    "israel-palestine": "Israel-Palestine",
    "education": "Education",
    "healthcare": "Healthcare",
    "free-speech": "Free Speech",
    "civil-rights": "Civil Rights & Equity",
    "russia-ukraine": "Russia & Ukraine",
    "general": "General",
}

TOPIC_EMOJIS = {
    "foreign-policy": "🌍",
    "immigration": "🛂",
    "climate": "🌱",
    "economy": "💰",
    "technology": "🤖",
    "guns": "🔫",
    "israel-palestine": "☮️",
    "education": "🎓",
    "healthcare": "🏥",
    "free-speech": "🗣",
    "civil-rights": "⚖️",
    "russia-ukraine": "🇺🇦",
    "general": "📋",
}


# ─── Voice stance inference ──────────────────────────────────────────────────

# We infer each voice's approximate topic stances from their tags.
# This is very rough but good enough for a POC.

PROGRESSIVE_TAGS = {
    "progressive", "liberal", "democratic", "democrat", "left", "center-left",
    "democratic socialist", "populist left", "progressive left", "bernie-adjacent",
    "democratic establishment", "progressive activist", "progressive policy",
    "progressive commentary", "progressive economics", "progressive organizing",
    "democratic senator", "democratic leadership", "democratic media", "rising democrat",
    "populist democrat", "swing state democrat", "centrist democrat", "progressive populist",
    "msnbc", "progressive movement", "democratic governor",
}

CONSERVATIVE_TAGS = {
    "conservative", "republican", "right", "center-right", "maga", "pro-trump",
    "maga conservative", "maga populist", "maga firebrand", "conservative populist",
    "conservative policy", "traditional conservative", "establishment republican",
    "business republican", "moderate conservative", "fiscal conservative",
    "house republican leadership", "republican senator", "republican leadership",
    "neoconservative", "far-right", "freedom caucus", "turning point usa",
    "fox news", "trump ally", "populist right", "populist conservative",
    "new right", "constitutional conservative", "social conservative",
    "rising republican", "gen z conservative", "black conservative",
    "youth right-wing", "south conservative", "maverick republican",
    "pragmatic republican", "republican strategy", "republican establishment",
    "trump campaign", "pro-america",
}

CENTRIST_TAGS = {
    "centrist", "moderate", "bipartisan", "nonpartisan", "non-partisan",
    "independent", "balanced", "pragmatic", "heterodox", "centrist analysis",
    "pragmatic centrist", "bridge-building",
}

LIBERTARIAN_TAGS = {
    "libertarian", "libertarian-leaning", "libertarian republican",
    "anti-establishment", "limited government", "free markets",
    "fiscal conservatism", "free-market policy",
}


def infer_voice_lean(voice):
    """Return (lean, classified) where lean is -1 (progressive) to +1 (conservative).
    classified is True if we have enough tags to determine lean."""
    tags = set(t.lower() for t in voice.get("tags", []))
    prog_count = len(tags & PROGRESSIVE_TAGS)
    cons_count = len(tags & CONSERVATIVE_TAGS)
    cent_count = len(tags & CENTRIST_TAGS)
    lib_count = len(tags & LIBERTARIAN_TAGS)

    classified = (prog_count + cons_count + cent_count + lib_count) > 0

    if not classified:
        return 0.0, False

    score = 0.0
    score += cons_count * 1.0
    score -= prog_count * 1.0
    score += lib_count * 0.3
    # centrist = 0

    total = prog_count + cons_count + cent_count + lib_count
    return max(-1.0, min(1.0, score / total)), True


def build_voice_topic_stances(voice):
    """
    Build approximate topic stances for a voice.
    Returns (stances_dict, classified_bool).
    """
    lean, classified = infer_voice_lean(voice)
    tags = set(t.lower() for t in voice.get("tags", []))

    stances = {}

    # Base: their general lean applies to most topics
    for topic in TOPIC_LABELS:
        stances[topic] = lean * 0.5  # Dampen; nobody is extreme on everything

    # Specific topic overrides based on tags
    if any(t in tags for t in ["anti-war", "anti-interventionist", "non-interventionist", "foreign policy restraint", "restraint"]):
        stances["foreign-policy"] = -0.6
    if any(t in tags for t in ["defense hawk", "foreign policy hawk", "hawkish", "national security"]):
        stances["foreign-policy"] = 0.6

    if any(t in tags for t in ["anti-immigration", "immigration enforcement", "immigration hardliner", "border hawk", "border enforcement", "border security"]):
        stances["immigration"] = 0.7
    if any(t in tags for t in ["immigration advocate", "immigrant rights", "pro-immigration", "immigration reform"]):
        stances["immigration"] = -0.7

    if any(t in tags for t in ["climate activism", "climate advocacy", "climate accountability", "climate science", "environmental justice", "environmental policy"]):
        stances["climate"] = -0.7
    if any(t in tags for t in ["anti-doomism"]):
        stances["climate"] = 0.3

    if any(t in tags for t in ["economic nationalism", "fiscal conservatism", "fiscal conservative", "free markets", "free-market policy"]):
        stances["economy"] = 0.5
    if any(t in tags for t in ["economic justice", "economic inequality", "pro-labor", "progressive economics", "worker power"]):
        stances["economy"] = -0.5

    if any(t in tags for t in ["ai ethics", "ai regulation", "ai risk", "tech regulation", "tech accountability", "tech critic", "humane tech"]):
        stances["technology"] = -0.4
    if any(t in tags for t in ["tech-conservative"]):
        stances["technology"] = 0.4

    if any(t in tags for t in ["second amendment", "gun"]):
        stances["guns"] = 0.7
    if any(t in tags for t in ["gun reform", "gun safety"]):
        stances["guns"] = -0.7

    if any(t in tags for t in ["pro-israel"]):
        stances["israel-palestine"] = 0.6
    if any(t in tags for t in ["palestine", "palestine solidarity"]):
        stances["israel-palestine"] = -0.6

    if any(t in tags for t in ["free speech", "anti-censorship", "anti-pc"]):
        stances["free-speech"] = 0.5
    if any(t in tags for t in ["content moderation"]):
        stances["free-speech"] = -0.3

    if any(t in tags for t in ["racial justice", "anti-racism education", "civil rights", "lgbtq+", "lgbtq+ rights", "social justice", "dei"]):
        stances["civil-rights"] = -0.6
    if any(t in tags for t in ["anti-dei", "anti-woke", "culture warrior"]):
        stances["civil-rights"] = 0.6

    return stances, classified


# ─── Matching engine ──────────────────────────────────────────────────────────

def compute_user_topic_stances(responses):
    """Group a user's poll responses by topic, average each.

    Response values are 0-1 (Likert scale: 0=strongly disagree, 1=strongly agree).
    We normalize to -1..1 to match voice stance scale.
    """
    topic_vals = defaultdict(list)
    topic_vals_raw = defaultdict(list)  # Keep 0-1 for display
    for r in responses:
        if r["question"] is None or r["response_value"] is None:
            continue
        topic = classify_topic(r["question"])
        val = float(r["response_value"])
        topic_vals_raw[topic].append(val)
        # Normalize: 0-1 -> -1..1
        normalized = val * 2 - 1
        topic_vals[topic].append(normalized)

    stances = {}
    for topic, vals in topic_vals.items():
        stances[topic] = sum(vals) / len(vals)

    return stances, topic_vals_raw


def match_voices(user_stances, voice_stances_map):
    """
    Find closest and most different voices.
    Returns (closest_5, different_3) as lists of (voice, similarity, overlapping_topics).
    """
    distances = []
    for voice_id, voice_data in voice_stances_map.items():
        v_stances = voice_data["stances"]
        voice = voice_data["voice"]

        # Find overlapping topics (topics where user has responses)
        overlapping = set(user_stances.keys()) & set(v_stances.keys())
        if len(overlapping) < 2:
            continue

        # Compute distance on overlapping topics
        # User stances are -1 to 1, voice stances are -1 to 1
        total_diff = 0
        topic_diffs = {}
        for t in overlapping:
            diff = abs(user_stances[t] - v_stances[t])
            total_diff += diff
            topic_diffs[t] = user_stances[t] - v_stances[t]

        avg_diff = total_diff / len(overlapping)
        similarity = max(0, 1 - avg_diff)  # 0 to 1

        # Find which topics they most align/differ on
        sorted_topics = sorted(topic_diffs.items(), key=lambda x: abs(x[1]))
        align_topics = [t for t, d in sorted_topics[:3]]
        differ_topics = [t for t, d in sorted_topics[-3:]]

        distances.append({
            "voice": voice,
            "similarity": similarity,
            "avg_diff": avg_diff,
            "align_topics": align_topics,
            "differ_topics": differ_topics,
        })

    distances.sort(key=lambda x: x["similarity"], reverse=True)
    closest = distances[:5]
    different = sorted(distances, key=lambda x: x["similarity"])[:3]

    return closest, different


def find_signature_positions(responses, question_avgs):
    """Find the 3 most extreme poll answers.
    Values are 0-1 scale. Extremeness = distance from 0.5 (neutral)."""
    extremes = []
    for r in responses:
        if r["question"] is None or r["response_value"] is None:
            continue
        val = float(r["response_value"])
        extremeness = abs(val - 0.5)  # How far from neutral
        avg_data = question_avgs.get(r["question"], {"avg": 0.5, "count": 0})
        extremes.append({
            "question": r["question"],
            "value": val,
            "extremeness": extremeness,
            "headline": r.get("story_headline", ""),
            "avg": float(avg_data["avg"]),
            "count": int(avg_data["count"]),
        })

    # Deduplicate by question, keep highest extremeness
    seen = set()
    unique = []
    for e in extremes:
        if e["question"] not in seen:
            seen.add(e["question"])
            unique.append(e)

    unique.sort(key=lambda x: x["extremeness"], reverse=True)
    return unique[:3]


def find_surprise_positions(user_stances, compass):
    """
    Topics where the user deviates from their compass prediction.
    Compass 0-1 where 0 = very progressive, 1 = very conservative.
    User stances are now -1..1 (normalized).
    """
    expected_lean = (compass - 0.5) * 2  # -1 to 1

    surprises = []
    for topic, stance in user_stances.items():
        if topic == "general":
            continue
        expected = expected_lean * 0.5  # Dampened expectation
        deviation = abs(stance - expected)
        if deviation > 0.3:  # Meaningful deviation
            direction = "more progressive" if stance < expected else "more conservative"
            surprises.append({
                "topic": topic,
                "stance": stance,
                "expected": expected,
                "deviation": deviation,
                "direction": direction,
            })

    surprises.sort(key=lambda x: x["deviation"], reverse=True)
    return surprises[:3]


# ─── Compass interpretation ──────────────────────────────────────────────────

def interpret_compass(position):
    """Return a text interpretation of the compass position."""
    if position < 0.25:
        return "You lean significantly progressive. You tend to favor government intervention, social equity, and institutional reform."
    elif position < 0.40:
        return "You lean progressive. You generally support social programs, climate action, and civil rights expansion, while questioning concentrated power."
    elif position < 0.47:
        return "You lean slightly left of center. You often side with progressive policies but show moderate instincts on select issues."
    elif position < 0.53:
        return "You sit near the center. You draw from both progressive and conservative ideas, depending on the issue."
    elif position < 0.60:
        return "You lean slightly right of center. You tend toward fiscal restraint and institutional stability, while staying open on social issues."
    elif position < 0.75:
        return "You lean conservative. You generally favor free markets, traditional values, and a strong national defense."
    else:
        return "You lean significantly conservative. You consistently prioritize limited government, individual liberty, and traditional institutions."


# ─── HTML generation ─────────────────────────────────────────────────────────

def response_label(val):
    """Convert 0-1 value to text label."""
    if val <= 0.125:
        return "Strongly Disagree"
    elif val <= 0.375:
        return "Disagree"
    elif val <= 0.625:
        return "Neutral"
    elif val <= 0.875:
        return "Agree"
    else:
        return "Strongly Agree"


def response_color(val):
    """Color for a 0-1 response value."""
    if val <= 0.25:
        return "#6C9BF2"  # Blue (disagree)
    elif val <= 0.45:
        return "#8CB4F0"
    elif val <= 0.55:
        return "#808080"  # Gray (neutral)
    elif val <= 0.75:
        return "#F2A06C"
    else:
        return "#FF6343"  # Coral (agree)


def compass_dot_position(position):
    """CSS left percentage for compass dot."""
    return max(4, min(96, position * 100))


def generate_profile_html(user, closest, different, signature, surprises, user_stances, topic_vals):
    """Generate a beautiful HTML profile page."""
    first_name = html_mod.escape((user["first_name"] or "").strip().title())
    polls_count = user["polls_answered_count"]
    compass = user["compass_position"]

    # Build topic bars HTML
    topic_bars_html = ""
    # Sort topics by number of responses
    sorted_topics = sorted(
        [(t, vals) for t, vals in topic_vals.items() if len(vals) >= 3 and t != "general"],
        key=lambda x: len(x[1]),
        reverse=True,
    )

    for topic, vals in sorted_topics:
        avg = sum(vals) / len(vals)  # 0-1 scale
        label = TOPIC_LABELS.get(topic, topic)
        emoji = TOPIC_EMOJIS.get(topic, "")
        count = len(vals)
        bar_pct = avg * 100  # Already 0-100
        bar_color = response_color(avg)
        lean_text = "leans disagree" if avg < 0.4 else "leans agree" if avg > 0.6 else "balanced"

        topic_bars_html += f'''
        <div style="margin-bottom:16px;">
          <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px;">
            <span style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:13px; color:#FFFFFF; font-weight:500;">{emoji} {label}</span>
            <span style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666;">{count} polls · {lean_text}</span>
          </div>
          <div style="height:6px; background:#1F1F1F; border-radius:3px; overflow:hidden; position:relative;">
            <div style="position:absolute; left:0; top:0; height:100%; width:{bar_pct:.0f}%; background:linear-gradient(90deg, #6C9BF2, {bar_color}); border-radius:3px;"></div>
            <div style="position:absolute; left:50%; top:-2px; width:1px; height:10px; background:#444;"></div>
          </div>
        </div>'''

    # Build closest voices HTML
    closest_html = ""
    for i, match in enumerate(closest):
        v = match["voice"]
        sim = match["similarity"]
        pct = int(sim * 100)
        name = html_mod.escape(v.get("name", v.get("id", "")))
        category = html_mod.escape(v.get("category", "").title())
        approach = html_mod.escape(v.get("approach", ""))
        photo_url = v.get("photo", "")
        # Use the Render URL for photos
        if photo_url.startswith("/photos/"):
            photo_url = f"https://newsreel-perspectives.onrender.com{photo_url}"
        align_topics = ", ".join(TOPIC_LABELS.get(t, t) for t in match["align_topics"][:2])
        lens = html_mod.escape((v.get("lens", "") or "")[:120])

        closest_html += f'''
        <div style="display:flex; align-items:center; padding:16px; background:#141416; border-radius:12px; margin-bottom:8px; border:1px solid #1F1F1F;">
          <div style="flex-shrink:0; margin-right:14px;">
            <img src="{photo_url}" width="48" height="48" style="width:48px; height:48px; border-radius:50%; object-fit:cover; display:block; background:#1F1F1F;" alt="{name}" onerror="this.style.display='none'">
          </div>
          <div style="flex:1; min-width:0;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
              <span style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:15px; color:#FFFFFF; font-weight:600;">{name}</span>
              <span style="font-family:'IBM Plex Mono',monospace; font-size:12px; color:#FF6343; font-weight:600;">{pct}%</span>
            </div>
            <div style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:4px;">{category} · {approach}</div>
            <div style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:12px; color:#888; line-height:1.4;">Closest on {align_topics}</div>
          </div>
        </div>'''

    # Build different voices HTML
    different_html = ""
    for match in different:
        v = match["voice"]
        sim = match["similarity"]
        pct = int(sim * 100)
        name = html_mod.escape(v.get("name", v.get("id", "")))
        category = html_mod.escape(v.get("category", "").title())
        approach = html_mod.escape(v.get("approach", ""))
        photo_url = v.get("photo", "")
        if photo_url.startswith("/photos/"):
            photo_url = f"https://newsreel-perspectives.onrender.com{photo_url}"
        differ_topics = ", ".join(TOPIC_LABELS.get(t, t) for t in match["differ_topics"][:2])

        different_html += f'''
        <div style="display:flex; align-items:center; padding:16px; background:#141416; border-radius:12px; margin-bottom:8px; border:1px solid #1F1F1F;">
          <div style="flex-shrink:0; margin-right:14px;">
            <img src="{photo_url}" width="48" height="48" style="width:48px; height:48px; border-radius:50%; object-fit:cover; display:block; background:#1F1F1F;" alt="{name}" onerror="this.style.display='none'">
          </div>
          <div style="flex:1; min-width:0;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
              <span style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:15px; color:#FFFFFF; font-weight:600;">{name}</span>
              <span style="font-family:'IBM Plex Mono',monospace; font-size:12px; color:#666; font-weight:600;">{pct}%</span>
            </div>
            <div style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#666; text-transform:uppercase; letter-spacing:0.8px; margin-bottom:4px;">{category} · {approach}</div>
            <div style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:12px; color:#888; line-height:1.4;">Diverge on {differ_topics}</div>
          </div>
        </div>'''

    # Build signature positions HTML
    signature_html = ""
    for sig in signature:
        q = html_mod.escape(sig["question"])
        val = sig["value"]
        avg = sig["avg"]
        label = response_label(val)
        avg_label = response_label(avg)
        color = response_color(val)
        headline = html_mod.escape(sig.get("headline", "") or "")

        # Visual: show user's answer vs crowd (values already 0-1)
        user_pct = val * 100
        avg_pct = avg * 100

        signature_html += f'''
        <div style="padding:20px; background:#141416; border-radius:12px; margin-bottom:12px; border:1px solid #1F1F1F;">
          <div style="font-family:'DM Sans',Helvetica,Arial,sans-serif; font-size:14px; color:#FFFFFF; line-height:1.4; margin-bottom:12px;">"{q}"</div>
          <div style="margin-bottom:8px;">
            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
              <span style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:{color}; text-transform:uppercase; letter-spacing:0.5px;">You: {label}</span>
            </div>
            <div style="height:4px; background:#1F1F1F; border-radius:2px; position:relative;">
              <div style="position:absolute; left:{user_pct:.0f}%; top:-3px; width:10px; height:10px; background:{color}; border-radius:50%; transform:translateX(-5px);"></div>
            </div>
          </div>
          <div>
            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
              <span style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#555; text-transform:uppercase; letter-spacing:0.5px;">Everyone: {avg_label}</span>
            </div>
            <div style="height:4px; background:#1F1F1F; border-radius:2px; position:relative;">
              <div style="position:absolute; left:{avg_pct:.0f}%; top:-3px; width:10px; height:10px; background:#444; border-radius:50%; transform:translateX(-5px);"></div>
            </div>
          </div>
          <div style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#444; margin-top:8px; display:flex; justify-content:space-between;">
            <span>Strongly Disagree</span><span>Neutral</span><span>Strongly Agree</span>
          </div>
        </div>'''

    # Compass visual
    dot_left = compass_dot_position(compass)
    compass_text = interpret_compass(compass)

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Perspective Profile - Newsreel</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bree+Serif&family=DM+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0a0a0b; color:#fff; font-family:'DM Sans',Helvetica,Arial,sans-serif; -webkit-font-smoothing:antialiased; }}
  .container {{ max-width:560px; margin:0 auto; padding:0 20px; }}
  .section {{ margin-bottom:40px; }}
  .section-label {{ font-family:'IBM Plex Mono',monospace; font-size:10px; color:#FF6343; text-transform:uppercase; letter-spacing:1.5px; font-weight:600; margin-bottom:16px; }}
  .divider {{ border:none; border-top:1px solid #1F1F1F; margin:32px 0; }}
  @media (max-width: 600px) {{
    .container {{ padding:0 16px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Accent line -->
  <div style="height:3px; background:#FF6343; margin:0 -20px;"></div>

  <!-- Header -->
  <div style="display:flex; justify-content:space-between; align-items:center; padding:24px 0 16px;">
    <div style="font-family:'Bree Serif',Georgia,serif; font-size:18px; color:#FFFFFF; letter-spacing:-0.3px;">newsreel</div>
    <div style="font-family:'IBM Plex Mono',monospace; font-size:10px; color:#555; text-transform:uppercase; letter-spacing:1px;">Perspective Profile</div>
  </div>

  <hr class="divider" style="margin-top:0;">

  <!-- Greeting -->
  <div class="section">
    <div style="font-family:'DM Sans',sans-serif; font-size:16px; color:#fff; line-height:1.6; margin-bottom:8px;">
      Hey {first_name},
    </div>
    <div style="font-family:'DM Sans',sans-serif; font-size:15px; color:#999; line-height:1.6;">
      You've answered <span style="color:#FF6343; font-weight:600;">{polls_count} polls</span> on Newsreel. Here's what we learned about how you see the world.
    </div>
  </div>

  <hr class="divider">

  <!-- Compass -->
  <div class="section">
    <div class="section-label">Your Compass</div>

    <div style="padding:24px; background:#141416; border-radius:16px; border:1px solid #1F1F1F;">
      <!-- Compass bar -->
      <div style="position:relative; height:32px; margin-bottom:16px;">
        <div style="position:absolute; top:12px; left:0; right:0; height:8px; background:linear-gradient(90deg, #6C9BF2 0%, #888 50%, #FF6343 100%); border-radius:4px; opacity:0.3;"></div>
        <div style="position:absolute; top:4px; left:{dot_left:.1f}%; transform:translateX(-50%);">
          <div style="width:24px; height:24px; border-radius:50%; background:#FF6343; border:3px solid #0a0a0b; box-shadow:0 0 12px rgba(255,99,67,0.4);"></div>
        </div>
      </div>
      <div style="display:flex; justify-content:space-between; margin-bottom:16px;">
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#6C9BF2; text-transform:uppercase; letter-spacing:0.5px;">Progressive</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#888; text-transform:uppercase; letter-spacing:0.5px;">Center</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#FF6343; text-transform:uppercase; letter-spacing:0.5px;">Conservative</span>
      </div>
      <div style="font-family:'DM Sans',sans-serif; font-size:13px; color:#999; line-height:1.5;">{compass_text}</div>
    </div>
  </div>

  <hr class="divider">

  <!-- Voices you align with -->
  <div class="section">
    <div class="section-label">Voices You Align With</div>
    <div style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; margin-bottom:16px;">
      Out of 257 tracked voices across politics, media, and culture.
    </div>
    {closest_html}
  </div>

  <hr class="divider">

  <!-- Voices that challenge you -->
  <div class="section">
    <div class="section-label">Voices That Challenge You</div>
    <div style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; margin-bottom:16px;">
      The perspectives furthest from your positions.
    </div>
    {different_html}
  </div>

  <hr class="divider">

  <!-- Signature positions -->
  <div class="section">
    <div class="section-label">Your Signature Positions</div>
    <div style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; margin-bottom:16px;">
      The polls where you had your strongest opinions, compared to how everyone else answered.
    </div>
    {signature_html}
  </div>

  <hr class="divider">

  <!-- Topic map -->
  <div class="section">
    <div class="section-label">Your Topic Map</div>
    <div style="font-family:'DM Sans',sans-serif; font-size:13px; color:#666; margin-bottom:16px;">
      Your average stance across topics where you've answered 3+ polls.
    </div>
    <div style="padding:20px; background:#141416; border-radius:16px; border:1px solid #1F1F1F;">
      {topic_bars_html}
      <div style="display:flex; justify-content:space-between; margin-top:8px; padding-top:8px; border-top:1px solid #1F1F1F;">
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#6C9BF2;">Disagree</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#888;">Neutral</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#FF6343;">Agree</span>
      </div>
    </div>
  </div>

  <hr class="divider">

  <!-- Footer -->
  <div style="text-align:center; padding:24px 0 48px;">
    <div style="font-family:'Bree Serif',Georgia,serif; font-size:14px; color:#FF6343; margin-bottom:8px;">Step outside your algorithm.</div>
    <div style="font-family:'DM Sans',sans-serif; font-size:12px; color:#555; margin-bottom:20px;">
      See all 257 voices at
      <a href="https://newsreel-perspectives.onrender.com" style="color:#FF6343; text-decoration:none;">newsreel-perspectives.onrender.com</a>
    </div>
    <div style="font-family:'IBM Plex Mono',monospace; font-size:9px; color:#333; text-transform:uppercase; letter-spacing:1px;">
      Newsreel Perspectives &middot; Proof of Concept
    </div>
  </div>

</div>
</body>
</html>'''

    return html


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n  Perspective Profiles Generator")
    print("  ==============================\n")

    # Load data
    users = load_users()
    responses = load_poll_responses()
    question_avgs = load_question_averages()
    voices = load_voices()

    print(f"  Loaded {len(users)} users, {len(responses)} poll responses, {len(voices)} voices\n")

    # Index responses by user
    user_responses = defaultdict(list)
    for r in responses:
        user_responses[r["user_id"]].append(r)

    # Build voice stances (only for voices we can classify politically)
    voice_stances_map = {}
    classified_count = 0
    for v in voices:
        stances, classified = build_voice_topic_stances(v)
        if classified:
            voice_stances_map[v["id"]] = {
                "voice": v,
                "stances": stances,
            }
            classified_count += 1
    print(f"  Classified voices for matching: {classified_count} / {len(voices)}")

    # Track stats
    all_closest_voices = []
    school_groups = defaultdict(list)
    profiles_generated = 0

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    for user in users:
        uid = user["id"]
        uresp = user_responses.get(uid, [])
        if not uresp:
            print(f"  [SKIP] {user['first_name']} - no responses found")
            continue

        # Compute user stances
        user_stances, topic_vals = compute_user_topic_stances(uresp)

        # Match voices
        closest, different = match_voices(user_stances, voice_stances_map)

        # Signature positions
        signature = find_signature_positions(uresp, question_avgs)

        # Surprise positions
        surprises = find_surprise_positions(user_stances, user["compass_position"])

        # Generate HTML
        html = generate_profile_html(user, closest, different, signature, surprises, user_stances, topic_vals)

        # Write file
        profile_path = PROFILES_DIR / f"{uid}.html"
        with open(profile_path, "w") as f:
            f.write(html)

        profiles_generated += 1

        # Track stats
        if closest:
            all_closest_voices.append(closest[0]["voice"]["name"])

        # Classify school
        email = user["email"]
        if "psu.edu" in email:
            school_groups["Penn State"].append(user)
        elif "nyu.edu" in email:
            school_groups["NYU"].append(user)
        elif "fairfield.edu" in email:
            school_groups["Fairfield"].append(user)
        elif "nycstudents.net" in email:
            school_groups["NYC Schools"].append(user)
        else:
            school_groups["Other"].append(user)

        name = (user["first_name"] or "").strip()
        if closest:
            top_voice = closest[0]["voice"]["name"]
            sim = int(closest[0]["similarity"] * 100)
            print(f"  [{profiles_generated:2d}] {name:12s} - compass {user['compass_position']:.2f} - top match: {top_voice} ({sim}%)")
        else:
            print(f"  [{profiles_generated:2d}] {name:12s} - compass {user['compass_position']:.2f} - no voice match")

    # ─── Summary ──────────────────────────────────────────────────────────────

    print(f"\n  {'='*50}")
    print(f"  SUMMARY")
    print(f"  {'='*50}\n")

    print(f"  Profiles generated: {profiles_generated}")
    print(f"  Output directory: {PROFILES_DIR}\n")

    # Most common closest voice
    from collections import Counter
    voice_counts = Counter(all_closest_voices)
    print(f"  Most common top match:")
    for voice, count in voice_counts.most_common(10):
        print(f"    {voice}: {count} users")

    # Average compass
    avg_compass = sum(u["compass_position"] for u in users) / len(users)
    print(f"\n  Average compass position: {avg_compass:.3f}")
    print(f"  (0 = very progressive, 0.5 = center, 1 = very conservative)")
    print(f"  Group leans: {'slightly progressive' if avg_compass < 0.47 else 'centrist' if avg_compass < 0.53 else 'slightly conservative'}")

    # School comparison
    print(f"\n  By school:")
    for school, members in sorted(school_groups.items()):
        avg = sum(u["compass_position"] for u in members) / len(members)
        print(f"    {school:15s} ({len(members):2d} users) - avg compass: {avg:.3f} {'(progressive)' if avg < 0.43 else '(moderate)' if avg < 0.50 else '(center-right)' if avg < 0.53 else '(conservative)'}")

    print(f"\n  Done! View profiles at /profile/{{user_id}}\n")


if __name__ == "__main__":
    main()
