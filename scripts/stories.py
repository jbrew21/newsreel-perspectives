#!/usr/bin/env python3
"""
Newsreel Perspectives — Unified Daily Stories

Produces a single homepage feed by:
1. Pulling today's CMS editorial stories (what the newsroom picked)
2. Finding auto-detected topics from voice data (what voices are buzzing about)
3. For each, matching voice posts and clustering arguments
4. Scoring & ranking by how interesting the voice coverage is

Output: data/posts/stories-YYYY-MM-DD.json

Usage:
  python scripts/stories.py                    # today
  python scripts/stories.py --date 2026-03-13  # specific date
"""

import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
POSTS_DIR = ROOT / "data" / "posts"
VOICES_PATH = ROOT / "data" / "voices.json"
CMS_API = "https://newsreel-cms.onrender.com/api"


def get_voice_photo(meta, voice_name):
    """Get photo URL from voice metadata, falling back to ui-avatars only if no real photo exists."""
    photo = meta.get('photo', '') if meta else ''
    # Use the real photo if it exists and isn't already a ui-avatars fallback
    if photo and 'ui-avatars.com' not in photo:
        return photo
    # Fallback: generate a ui-avatars URL from the voice name
    encoded = urllib.parse.quote(voice_name)
    return f"https://ui-avatars.com/api/?name={encoded}&background=252528&color=a1a1aa&size=96"


def load_env():
    for env_path in [ROOT / ".env", ROOT.parent / "newsletter" / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    key, _, val = line.partition('=')
                    if key.strip() not in os.environ:
                        os.environ[key.strip()] = val.strip()

load_env()
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


def call_claude(prompt, max_tokens=1024):
    """Call Claude API and return parsed JSON from response."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=json.dumps({
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode(),
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        result_text = data.get('content', [{}])[0].get('text', '')
        json_match = re.search(r'\{[\s\S]*\}', result_text)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print(f"  Claude API error: {e}")


# Cluster name normalization: standardize synonyms
CLUSTER_NAME_MAP = {
    'media coverage critique': 'Media Criticism',
    'media critique': 'Media Criticism',
    'media accountability': 'Media Criticism',
    'press criticism': 'Media Criticism',
    'anti-media': 'Media Criticism',
    'media skepticism': 'Media Criticism',
}


def normalize_cluster_name(name):
    """Normalize cluster name for consistency across stories."""
    low = name.strip().lower()
    if low in CLUSTER_NAME_MAP:
        return CLUSTER_NAME_MAP[low]
    # Title case
    return name.strip().title() if name == name.lower() else name.strip()
    return None


# ── Step 1: Gather candidate stories ────────────────────────────

def get_cms_stories(date):
    """Pull today's editorial stories from CMS."""
    stories = []
    for endpoint in [
        f"{CMS_API}/newsreels/{date}",
        f"{CMS_API}/stories?status=published&date={date}&sort=newest&limit=10",
    ]:
        try:
            req = urllib.request.Request(endpoint)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            # newsreels endpoint wraps in { stories: [...] }
            raw = data.get('stories', [data] if isinstance(data, dict) else data)
            if isinstance(raw, list) and len(raw) > 0:
                for s in raw:
                    if isinstance(s, dict) and s.get('headline', s.get('story_headline', '')):
                        stories.append({
                            'headline': s.get('headline', s.get('story_headline', '')),
                            'subhead': s.get('subhead', ''),
                            'cover_url': s.get('cover_url', ''),
                            'story_type': s.get('story_type', s.get('type', '')),
                            'source': 'cms',
                        })
                if stories:
                    break
        except Exception as e:
            print(f"  CMS fetch failed ({endpoint}): {e}")
            continue
    return stories


def get_voice_topics(date, min_voices=4, max_topics=8):
    """Find topics with enough voice coverage from recent data (48h window)."""
    index_path = POSTS_DIR / f'topic-index-{date}.json'
    if not index_path.exists():
        return {}, {}

    topic_index = json.loads(index_path.read_text())

    # Time-scope: only include posts from last 48 hours
    try:
        cutoff = datetime.strptime(date, '%Y-%m-%d') - timedelta(hours=48)
    except:
        cutoff = datetime.now() - timedelta(hours=48)

    SKIP = {'uncategorized', 'other'}

    topics = {}
    filtered_index = {}
    for topic, entries in topic_index.items():
        if topic in SKIP:
            continue

        # Filter to recent posts only
        recent = []
        for e in entries:
            ts = e.get('timestamp', '')
            if ts:
                try:
                    # Handle various timestamp formats
                    post_time = datetime.fromisoformat(ts.replace('Z', '+00:00').replace('+00:00', ''))
                    if post_time.replace(tzinfo=None) < cutoff:
                        continue
                except:
                    pass  # Keep posts with unparseable timestamps
            recent.append(e)

        if not recent:
            continue

        filtered_index[topic] = recent

        unique_voices = {}
        for e in recent:
            vid = e['voiceId']
            if vid not in unique_voices:
                unique_voices[vid] = e
        if len(unique_voices) >= min_voices:
            topics[topic] = unique_voices

    return topics, filtered_index


# ── Step 2: Match CMS stories to voice topics ──────────────────

def match_cms_to_voices(cms_stories, voice_topics):
    """Use Claude to match CMS headlines to voice topic slugs."""
    if not cms_stories or not voice_topics:
        return {}

    headlines = [s['headline'] for s in cms_stories]
    topic_list = list(voice_topics.keys())

    prompt = f"""Match these news headlines to the most relevant topic slugs.

Headlines:
{json.dumps(headlines, indent=2)}

Available topic slugs:
{json.dumps(topic_list, indent=2)}

For each headline, return the 1-3 most relevant topic slugs (or empty array if none match).

Return ONLY this JSON:
{{
  "matches": {{
    "headline text": ["topic-slug-1", "topic-slug-2"],
    "headline text 2": []
  }}
}}"""

    result = call_claude(prompt, max_tokens=1024)
    if result and 'matches' in result:
        return result['matches']
    return {}


# ── Step 3: Analyze voices on a story ───────────────────────────

def analyze_voices(headline, voices_data, voices_meta):
    """Cluster voices and generate insight for a story."""
    summaries = []
    for vid, entry in voices_data.items():
        meta = voices_meta.get(vid, {})
        quote = entry.get('quote', entry.get('text', ''))[:250]
        bio = meta.get('lens', 'commentator')
        name = entry.get('voiceName', vid)
        platform = entry.get('platform', '')
        # Flag YouTube title-only posts so Claude knows they lack opinion text
        is_title_only = (platform == 'youtube' and len(quote) < 100
                         and not any(c in quote for c in '.!?'))
        if is_title_only:
            summaries.append(f"- {name} ({bio}): [VIDEO TITLE: \"{quote}\"] (covering this topic, but no direct quote available)")
        else:
            summaries.append(f"- {name} ({bio}): \"{quote}\"")

    voices_block = '\n'.join(summaries[:30])

    prompt = f"""Analyze what these public voices are saying about this story: "{headline}"

Voices and their quotes:
{voices_block}

Do these things:

1. HEADLINE: Write a short, specific news headline (under 12 words) summarizing what is actually happening. Ground the reader in the current story.

2. CLUSTER: Group these voices into 2-5 argument clusters. Each cluster is a distinct position or reaction. Name each in 2-4 words describing the ARGUMENT (not ideology). If there's no real split, use descriptive groupings like "Cautious Support" or "Demanding Action."
   CRITICAL: Name each cluster using language its MEMBERS would use to describe themselves, not language their opponents would use. "Deterrence Advocates" not "War Hawks". "Abortion Rights Defenders" not "Baby Killers". "Immigration Enforcement" not "Xenophobes". Always use neutral-to-sympathetic framing for every cluster.

3. ASSIGN: Put every voice in exactly one cluster. For voices marked [VIDEO TITLE], you can still assign them based on who they are and what the title suggests, but weight voices with actual quotes more heavily when determining cluster names and the summary.

4. SUMMARY: Write ONE sentence (under 20 words) capturing the most interesting thing about how voices are reacting. This could be:
   - A surprising split: "Left and right unite against the bill"
   - A consensus: "Rare agreement across the spectrum"
   - An interesting reaction: "12 voices weigh in, most demanding accountability"
   Don't force a "split" framing if there isn't one. Just describe what's happening.

5. TYPE: Classify as one of: "split" (clear opposing camps), "spectrum" (range of views), "consensus" (broad agreement), "reaction" (mostly one-directional response)

6. RELEVANCE: For EACH voice, rate whether their quote is actually about this specific story:
   - "direct" = clearly discussing this exact story/event
   - "related" = discussing the broader topic but not this specific story
   - "unrelated" = not relevant at all
   Count how many are "direct" vs total. This is critical for data quality.

7. CONFIDENCE: Rate 1-10 how confident you are that these voices are genuinely reacting to the SAME story (not just the same broad topic). 1 = voices are scattered across unrelated topics. 10 = every voice is clearly discussing the same event.

Return ONLY this JSON:
{{
  "headline": "The specific news headline",
  "clusters": {{
    "cluster name": ["Voice Name 1", "Voice Name 2"],
    "cluster name 2": ["Voice Name 3"]
  }},
  "summary": "The one-liner about the conversation",
  "type": "split|spectrum|consensus|reaction",
  "relevance": {{"direct": 8, "related": 12, "unrelated": 3}},
  "confidence": 7
}}"""

    return call_claude(prompt, max_tokens=1024)


def validate_clusters(headline, clusters, voices_data, voices_meta):
    """Second-pass validation: rate how well each voice fits its assigned cluster."""
    # Build voice-cluster pairs with quotes
    assignments = []
    for cluster_name, voice_names in clusters.items():
        for name in voice_names:
            quote = ''
            for vid, entry in voices_data.items():
                entry_name = entry.get('voiceName', vid)
                if entry_name.lower() == name.lower() or vid == name.lower().replace(' ', '-'):
                    quote = entry.get('quote', entry.get('text', ''))[:250]
                    break
            assignments.append(f'- {name} -> cluster "{cluster_name}": "{quote}"')

    assignments_block = '\n'.join(assignments)

    prompt = f"""Here are voice quotes assigned to argument clusters about this story: "{headline}"

For each voice, rate how well their quote actually supports their cluster assignment.
A voice saying "this is terrible" assigned to "Supporters" would be a 1.
A voice clearly arguing the cluster's position would be a 10.

Assignments:
{assignments_block}

For each voice, return a fit score (1-10) and a one-line reason.

Return ONLY this JSON:
{{
  "validations": [
    {{"voice": "Voice Name", "cluster": "cluster name", "fit": 7, "reason": "quote clearly argues this position"}}
  ]
}}"""

    return call_claude(prompt, max_tokens=1024)


def update_cluster_history(stories, date):
    """Append voice-cluster assignments to cluster-history.json for temporal tracking."""
    history_path = ROOT / "data" / "cluster-history.json"

    # Load existing history
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
        except Exception:
            history = {}
    else:
        history = {}

    for story in stories:
        headline = story.get('headline', '')
        topic_slugs = story.get('topicSlugs', [])
        topic_slug = topic_slugs[0] if topic_slugs else 'unknown'

        for cluster in story.get('clusters', []):
            cluster_name = cluster.get('name', '')
            for voice in cluster.get('voices', []):
                voice_id = voice.get('voiceId', '')
                if not voice_id:
                    continue

                if voice_id not in history:
                    history[voice_id] = {}
                if topic_slug not in history[voice_id]:
                    history[voice_id][topic_slug] = []

                # Find fit score from validation data if available
                fit = voice.get('fit', None)

                entry = {
                    'date': date,
                    'cluster': cluster_name,
                    'headline': headline,
                }
                if fit is not None:
                    entry['fit'] = fit

                history[voice_id][topic_slug].append(entry)

    history_path.write_text(json.dumps(history, indent=2))
    print(f"  Updated cluster history: {history_path}")


# ── Step 4: Build the unified feed ──────────────────────────────

def build_stories(date=None):
    """Build the unified daily stories feed."""
    if not date:
        date = datetime.now().strftime('%Y-%m-%d')

    print(f"\n  Building stories feed for {date}...")

    # Load voice metadata
    voices_meta = {}
    try:
        voices_list = json.loads(VOICES_PATH.read_text())
        voices_meta = {v['id']: v for v in voices_list}
    except Exception:
        pass

    # 1. Get CMS stories and voice topics
    cms_stories = get_cms_stories(date)
    voice_topics, topic_index = get_voice_topics(date, min_voices=4)

    print(f"  CMS stories: {len(cms_stories)}")
    print(f"  Voice topics (4+ voices): {len(voice_topics)}")

    if not voice_topics:
        print("  No voice topics found. Exiting.")
        return

    # 2. Match CMS stories to voice topics
    cms_matches = {}
    if cms_stories:
        print(f"\n  Matching CMS stories to voice data...")
        cms_matches = match_cms_to_voices(cms_stories, voice_topics)
        for headline, topics in cms_matches.items():
            print(f"    {headline[:60]}... -> {topics}")

    # 3. Build candidate list (CMS stories with voice matches + pure voice topics)
    candidates = []
    used_topics = set()

    # CMS stories that matched voice topics
    # Skip overly broad categories that would pull in unrelated voices
    BROAD_TOPICS = {'other', 'culture-war', 'media-press', 'celebrity-entertainment'}

    for story in cms_stories:
        matched_topics = cms_matches.get(story['headline'], [])
        # Filter out broad catch-all topics
        matched_topics = [t for t in matched_topics if t not in BROAD_TOPICS]
        if not matched_topics:
            continue

        # Merge voice data from all matched topics
        merged_voices = {}
        for topic_slug in matched_topics:
            if topic_slug in voice_topics:
                merged_voices.update(voice_topics[topic_slug])
                used_topics.add(topic_slug)

        if len(merged_voices) >= 3:
            candidates.append({
                'headline': story['headline'],
                'cover_url': story.get('cover_url', ''),
                'story_type': story.get('story_type', ''),
                'source': 'editorial',
                'topic_slugs': matched_topics,
                'voices': merged_voices,
                'voice_count': len(merged_voices),
            })

    # Voice-only topics (not already used by CMS stories)
    # Also skip broad catch-all topics for voice-driven stories
    VOICE_SKIP = BROAD_TOPICS | {'sports', 'education', 'healthcare'}
    for topic, voices in voice_topics.items():
        if topic in used_topics or topic in VOICE_SKIP:
            continue
        candidates.append({
            'headline': topic.replace('-', ' ').title(),  # placeholder, Claude will rewrite
            'cover_url': '',
            'story_type': '',
            'source': 'voices',
            'topic_slugs': [topic],
            'voices': voices,
            'voice_count': len(voices),
        })

    # Sort by voice count
    candidates.sort(key=lambda c: -c['voice_count'])

    # Take top 14
    candidates = candidates[:14]

    print(f"\n  Analyzing {len(candidates)} candidates:")
    for c in candidates:
        src = '[CMS]' if c['source'] == 'editorial' else '[voices]'
        print(f"    {src} {c['headline'][:60]} ({c['voice_count']} voices)")

    # 4. Analyze each candidate
    stories = []
    for candidate in candidates:
        print(f"\n  Clustering: {candidate['headline'][:50]}... ({candidate['voice_count']} voices)")

        result = analyze_voices(candidate['headline'], candidate['voices'], voices_meta)
        if not result or 'clusters' not in result:
            print(f"    Skipped (analysis failed)")
            continue

        # Quality gate: drop stories where voices aren't actually about this story
        confidence = result.get('confidence', 5)
        relevance = result.get('relevance', {})
        direct = relevance.get('direct', 0)
        related = relevance.get('related', 0)
        unrelated = relevance.get('unrelated', 0)
        total_rated = direct + related + unrelated or 1
        direct_pct = direct / total_rated

        print(f"    Quality: confidence={confidence}/10, direct={direct}/{total_rated} ({direct_pct:.0%})")

        if confidence <= 3:
            print(f"    DROPPED: confidence too low ({confidence}/10)")
            continue
        if direct_pct < 0.2 and direct < 3:
            print(f"    DROPPED: too few direct voices ({direct} direct, {direct_pct:.0%})")
            continue

        # Validation pass: check how well each voice fits its cluster
        validation_result = validate_clusters(
            candidate['headline'], result['clusters'],
            candidate['voices'], voices_meta
        )
        fit_scores = {}  # (voice_name_lower, cluster_name) -> {fit, reason}
        if validation_result and 'validations' in validation_result:
            for v in validation_result['validations']:
                key = (v.get('voice', '').lower(), v.get('cluster', ''))
                fit_scores[key] = {'fit': v.get('fit', 5), 'reason': v.get('reason', '')}
            low_fit = [v for v in validation_result['validations'] if v.get('fit', 5) < 4]
            if low_fit:
                print(f"    Validation: {len(low_fit)} voices dropped (fit < 4)")
                for v in low_fit:
                    print(f"      - {v.get('voice')} in '{v.get('cluster')}' (fit={v.get('fit')}): {v.get('reason', '')}")
        else:
            print(f"    Validation: skipped (API call failed)")

        # Remove low-fit voices from clusters before building output
        validated_clusters = {}
        for cluster_name, voice_names in result['clusters'].items():
            kept = []
            for name in voice_names:
                key = (name.lower(), cluster_name)
                score = fit_scores.get(key, {}).get('fit', 5)
                if score >= 4:
                    kept.append(name)
            if kept:
                validated_clusters[cluster_name] = kept
        result['clusters'] = validated_clusters

        # Build cluster objects with full voice data
        cluster_list = []
        for cluster_name, voice_names in result['clusters'].items():
            cluster_voices = []
            for name in voice_names:
                for vid, entry in candidate['voices'].items():
                    entry_name = entry.get('voiceName', vid)
                    if entry_name.lower() == name.lower() or vid == name.lower().replace(' ', '-'):
                        meta = voices_meta.get(vid, {})
                        voice_obj = {
                            'voiceId': vid,
                            'voiceName': entry_name,
                            'photo': get_voice_photo(meta, entry_name),
                            'quote': entry.get('quote', entry.get('text', ''))[:200],
                            'sourceUrl': entry.get('sourceUrl', ''),
                            'platform': entry.get('platform', ''),
                        }
                        # Attach fit score from validation
                        key = (name.lower(), cluster_name)
                        if key in fit_scores:
                            voice_obj['fit'] = fit_scores[key]['fit']
                        cluster_voices.append(voice_obj)
                        break

            if cluster_voices:
                cluster_list.append({
                    'name': normalize_cluster_name(cluster_name),
                    'voices': cluster_voices,
                    'voiceCount': len(cluster_voices),
                })

        cluster_list.sort(key=lambda c: -c['voiceCount'])

        # ── Apply editorial overrides from review dashboard ──
        overrides_path = ROOT / "data" / "editorial-overrides.json"
        if overrides_path.exists():
            try:
                overrides = json.loads(overrides_path.read_text())
                # Overrides keyed by headline -> {old_name: new_name}
                story_overrides = overrides.get(candidate['headline'], {})
                if story_overrides:
                    for cluster in cluster_list:
                        if cluster['name'] in story_overrides:
                            old_name = cluster['name']
                            cluster['name'] = story_overrides[old_name]
                            cluster['overridden'] = True
                            print(f"    Editorial override: '{old_name}' -> '{cluster['name']}'")
            except Exception:
                pass

        # ── Quote Quality Ranking ──
        # Rank voices within each cluster by quote quality tier
        PLATFORM_QUALITY = {
            'substack': 5,   # Long-form written opinion
            'bluesky': 4,    # Written post with stance
            'x': 3,          # Tweet — short but direct
            'instagram': 2,  # Caption, often visual-first
            'youtube': 1,    # Often just video title
            'tiktok': 1,     # Often just video title
            'podcast': 4,    # Transcript excerpt
        }
        for cluster in cluster_list:
            for voice in cluster['voices']:
                plat = voice.get('platform', '').lower()
                quote = voice.get('quote', '')
                # Base score from platform
                tier = PLATFORM_QUALITY.get(plat, 2)
                # Boost for longer, more substantive quotes
                if len(quote) > 100:
                    tier += 1
                # Boost for quotes with clear opinion markers
                if any(w in quote.lower() for w in ['because', 'should', 'must', 'wrong', 'right', 'dangerous', 'important']):
                    tier += 1
                voice['quoteQuality'] = min(tier, 7)
            # Sort voices by quality (best first)
            cluster['voices'].sort(key=lambda v: -v.get('quoteQuality', 0))
            # Surface best quote for the cluster
            if cluster['voices']:
                best = cluster['voices'][0]
                cluster['bestQuote'] = {
                    'voiceName': best['voiceName'],
                    'quote': best['quote'],
                    'platform': best['platform'],
                    'quality': best.get('quoteQuality', 0),
                }

        # ── Counter-Narrative Detection (semantic) ──
        # Find the two clusters most in tension, not just largest vs smallest
        counter_narrative = None
        if len(cluster_list) >= 2:
            cluster_names = [c['name'] for c in cluster_list]
            cluster_sizes = {c['name']: c['voiceCount'] for c in cluster_list}
            tension_prompt = f"""Given these argument clusters about "{result.get('headline', candidate['headline'])}":
{json.dumps(cluster_names)}

Which two clusters are most directly in TENSION or OPPOSITION to each other?
Not just different topics — actual disagreement on the same question.

Return ONLY this JSON:
{{"clusterA": "name", "clusterB": "name", "axis": "what they disagree about in 3-5 words", "tension": 8}}
tension = 1-10 how opposed they are (1=tangential, 10=direct opposition)
If no real tension exists, return {{"tension": 0}}"""

            tension_result = call_claude(tension_prompt, max_tokens=256)
            if tension_result and tension_result.get('tension', 0) >= 5:
                a_name = tension_result.get('clusterA', '')
                b_name = tension_result.get('clusterB', '')
                a_count = cluster_sizes.get(a_name, 0)
                b_count = cluster_sizes.get(b_name, 0)
                # Dominant = larger cluster, counter = smaller
                if a_count >= b_count:
                    dom_name, dom_count = a_name, a_count
                    ctr_name, ctr_count = b_name, b_count
                else:
                    dom_name, dom_count = b_name, b_count
                    ctr_name, ctr_count = a_name, a_count
                if ctr_count >= 2:
                    counter_narrative = {
                        'dominantCluster': dom_name,
                        'dominantCount': dom_count,
                        'counterCluster': ctr_name,
                        'counterCount': ctr_count,
                        'axis': tension_result.get('axis', ''),
                        'tensionScore': tension_result.get('tension', 0),
                        'tension': f"{dom_count} voices say \"{dom_name}\" — but {ctr_count} push back: \"{ctr_name}\"",
                    }

        # ── Story Heat Score ──
        # Composite: voice density + disagreement + cross-pollination + directness + confidence
        total_voices = sum(c['voiceCount'] for c in cluster_list)

        # Log scale for voice count (differentiates 10 vs 50 voices)
        voice_score = min(math.log(total_voices + 1) / math.log(50), 1.0)

        # Shannon entropy of cluster sizes (higher = more disagreement)
        if total_voices > 0 and len(cluster_list) > 1:
            proportions = [c['voiceCount'] / total_voices for c in cluster_list]
            entropy = -sum(p * math.log2(p) for p in proportions if p > 0)
            max_entropy = math.log2(len(cluster_list))
            disagreement = entropy / max_entropy if max_entropy > 0 else 0
        else:
            disagreement = 0

        # Ideological cross-pollination: do voices with opposing tags share a cluster?
        cross_score = 0.0
        if voices_meta:
            for cluster in cluster_list:
                cluster_tags = set()
                for voice in cluster.get('voices', []):
                    vid = voice.get('voiceId', '')
                    meta = voices_meta.get(vid, {})
                    for tag in meta.get('tags', []):
                        cluster_tags.add(tag.lower())
                # Check for ideological opposites in same cluster
                opposites = [
                    ({'conservative', 'right-leaning', 'maga', 'republican'}, {'progressive', 'left-leaning', 'democrat', 'liberal'}),
                    ({'pro-trump', 'trump-supporter'}, {'anti-trump', 'trump-critic'}),
                    ({'libertarian', 'libertarian-leaning'}, {'socialist', 'democratic-socialist'}),
                ]
                for set_a, set_b in opposites:
                    if cluster_tags & set_a and cluster_tags & set_b:
                        cross_score = 1.0  # Strange bedfellows found
                        break
                if cross_score > 0:
                    break

        directness_score = direct_pct  # from relevance check above
        conf_factor = min(confidence / 10.0, 1.0)

        heat_score = round(
            (voice_score * 0.25 + disagreement * 0.25 + cross_score * 0.2 + directness_score * 0.15 + conf_factor * 0.15) * 100
        )

        story = {
            'headline': result.get('headline', candidate['headline']),
            'summary': result.get('summary', ''),
            'type': result.get('type', 'spectrum'),
            'source': candidate['source'],
            'coverUrl': candidate.get('cover_url', ''),
            'storyType': candidate.get('story_type', ''),
            'topicSlugs': candidate['topic_slugs'],
            'voiceCount': candidate['voice_count'],
            'clusterCount': len(cluster_list),
            'clusters': cluster_list,
            'confidence': confidence,
            'relevance': relevance,
            'validated': bool(validation_result and 'validations' in validation_result),
            'heatScore': heat_score,
        }
        if counter_narrative:
            story['counterNarrative'] = counter_narrative

        stories.append(story)
        print(f"    [{result.get('type', '?')}] {len(cluster_list)} clusters: {', '.join(c['name'] for c in cluster_list)}")
        print(f"    Heat: {heat_score}/100 | {result.get('summary', '')}")
        if counter_narrative:
            print(f"    Counter: {counter_narrative['tension']}")

    # Sort stories by heat score (hottest first)
    stories.sort(key=lambda s: -s.get('heatScore', 0))

    # Save
    output_path = POSTS_DIR / f'stories-{date}.json'
    output_path.write_text(json.dumps(stories, indent=2))
    print(f"\n  Saved {len(stories)} stories to {output_path}")

    # Also save as fractures for backward compat
    compat_path = POSTS_DIR / f'fractures-{date}.json'
    compat = []
    for s in stories:
        compat.append({
            'topic': (s['topicSlugs'] or [''])[0],
            'topicDisplay': (s['topicSlugs'] or [''])[0].replace('-', ' ').title(),
            'headline': s['headline'],
            'voiceCount': s['voiceCount'],
            'clusterCount': s['clusterCount'],
            'insight': s['summary'],
            'clusters': s['clusters'],
        })
    compat_path.write_text(json.dumps(compat, indent=2))

    # Update temporal cluster history
    update_cluster_history(stories, date)

    return stories


def main():
    args = sys.argv[1:]
    date = None
    if '--date' in args:
        idx = args.index('--date')
        if idx + 1 < len(args):
            date = args[idx + 1]
    build_stories(date)


if __name__ == '__main__':
    main()
