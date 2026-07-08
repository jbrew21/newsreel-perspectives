#!/usr/bin/env python3
"""
Newsreel Perspectives — Search helpers

Pure, dependency-free logic for the search path so it can be unit-tested
without booting the HTTP server or hitting any live API:

- ``stories_for_topics`` joins curated homepage stories to a search's matched
  topics (so search can pin "the Newsreel take" above the voice feed).
- ``parse_timestamp`` / ``voice_latest_timestamp`` / ``sort_voices_by_recency``
  turn the per-quote ``timestamp`` (captured but historically unused) into a
  recency ordering.

None of these call the network; they operate on already-loaded dicts.
"""

import re
from datetime import datetime, timezone

# A timestamp floor for anything we cannot parse, so undated items sort last
# under a recency sort instead of crashing or jumping to the top.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def result_slug(text):
    """Slug used to name a prebuilt data/results/<slug>.json file.

    The lookup pipeline saves results under this slug and the server's
    offline fallback looks them up by it, so BOTH sides must derive it
    identically. Kept here as the single source of truth."""
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower())[:50]


def parse_timestamp(value):
    """Best-effort parse of an ISO-8601 timestamp to an aware ``datetime``.

    Handles the trailing ``Z``, timezone offsets, and fractional seconds.
    Returns ``_EPOCH`` (never None) for empty/unparseable values so callers can
    sort without special-casing. Naive datetimes are treated as UTC.
    """
    if not value or not isinstance(value, str):
        return _EPOCH
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fall back to a plain date (e.g. "2026-07-08").
        try:
            dt = datetime.fromisoformat(text[:10])
        except ValueError:
            return _EPOCH
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def voice_latest_timestamp(voice):
    """Return the most recent quote timestamp for a voice as a ``datetime``."""
    quotes = voice.get("quotes") or []
    if not quotes:
        return _EPOCH
    return max(parse_timestamp(q.get("timestamp")) for q in quotes)


def sort_quotes_by_recency(quotes):
    """Return quotes newest-first (stable for equal/missing timestamps)."""
    return sorted(quotes or [], key=lambda q: parse_timestamp(q.get("timestamp")), reverse=True)


def sort_voices_by_recency(voices):
    """Return voices newest-first by their most recent quote.

    Does not mutate the input list or its members.
    """
    return sorted(voices or [], key=voice_latest_timestamp, reverse=True)


def _story_topics(story):
    """The set of topic slugs a story is tagged with (tolerant of shape)."""
    slugs = story.get("topicSlugs") or story.get("topics") or []
    return {str(s).lower() for s in slugs if s}


def stories_for_topics(stories, matched_topics, limit=2):
    """Return curated stories relevant to a search's matched topics.

    A story is relevant when its ``topicSlugs`` overlaps ``matched_topics``.
    Results are ranked by overlap size, then by voice count, so the most
    on-point and most-covered story pins first. Returns at most ``limit``.
    """
    if not stories or not matched_topics:
        return []
    wanted = {str(t).lower() for t in matched_topics if t}
    if not wanted:
        return []

    scored = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        overlap = _story_topics(story) & wanted
        if overlap:
            scored.append((len(overlap), story.get("voiceCount") or 0, story))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [s for _, _, s in scored[:limit]]
