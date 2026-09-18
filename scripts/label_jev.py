#!/usr/bin/env python3
"""
Post labeling via TypeSafe Jev (System One), the classification half of what
Haiku does today in collect.py Phase 3.

Jev returns typed decisions and calibrated probabilities. It does NOT generate
text, so it cannot write the 4-8 word `summary`. That stays on Haiku and runs
only for posts that survive the filter here (see JEV-MIGRATION-PLAN.md §3.2).

Nothing in this module touches the live pipeline. collect.py reaches it only
when LABELER is "shadow" or "jev"; the default path is unchanged.

  API:  POST https://api.typesafe.ai/v1/systemone
        Authorization: Bearer $TYPESAFE_API_KEY

Two designs are implemented because the plan says pick on measured agreement,
not on theory:

  A  one `choice` over the 41 taxonomy slugs, secondaries off the probabilities
  B  41 `noul` questions, one per slug, keep every slug at or above threshold

Run `scripts/shadow_compare.py` to score them against Haiku's existing labels.
"""

import concurrent.futures
import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load .env the same way the rest of the pipeline does (migrate_to_supabase.py,
# collect.py). Must run before the module constants below, which read the
# environment at import time. A real environment variable always wins, so CI
# secrets override the local file.
for _env_path in [ROOT / '.env', ROOT.parent / 'newsletter' / '.env']:
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                if _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()

API_URL = "https://api.typesafe.ai/v1/systemone"

# "jev-latest" is what the docs document. The response echoes the resolved
# version back in `model`, and that echoed value is what we record per post as
# `label_model`, so the corpus carries a real pin without us guessing a version
# string. Override with JEV_MODEL to pin the request itself.
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

DESIGN = os.environ.get("JEV_DESIGN", "A").upper()

# ─── Thresholds (the dials shadow mode tunes) ────────────────────────────────
# A post is kept when it is probably news AND the author probably takes a
# position. 0.50 approximates today's argmax rule; it is not identical, because
# argmax can land on "low" while P(medium)+P(high) still clears 0.50. Both the
# argmax label and the probability are recorded so shadow mode can measure the
# gap instead of assuming it away.
KEEP_RELEVANCE_P = float(os.environ.get("JEV_KEEP_RELEVANCE_P", "0.50"))
KEEP_STANCE_P = float(os.environ.get("JEV_KEEP_STANCE_P", "0.50"))

TOPIC_SECONDARY_P = float(os.environ.get("JEV_TOPIC_SECONDARY_P", "0.20"))
NOUL_TOPIC_P = float(os.environ.get("JEV_NOUL_TOPIC_P", "0.50"))
TOPIC_MAX = 3

POST_CHARS = 300          # same truncation as the Haiku prompt
MAX_WORKERS = int(os.environ.get("JEV_WORKERS", "8"))
MAX_RETRIES = 4
TIMEOUT = 30

RELEVANCE_LEVELS = ["low", "medium", "high"]
STANCE_LEVELS = ["neutral", "lean", "strong"]

RELEVANCE_CRITERIA = [
    "Personal, promotional, or entertainment content with no news story",
    "Tangentially related to a current news story",
    "Clearly about a specific current news story",
]

STANCE_CRITERIA = [
    "Purely informational: a summary, both-sides reporting, or a link with no view",
    # Spec correction (Sep 18): the plan's own text for this level read
    # "...but is not stated", which violates the plan's no-negations rule.
    # The ordering against level 2 already carries the implicit/explicit
    # distinction, so the negative clause was dropped rather than reworded.
    "A position is implied by framing, word choice, or tone",
    "A clear opinion, argument, criticism, praise, or call to action",
]

_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "retries": 0, "failures": 0}


def usage_stats():
    """Counters for log_usage(). Contains no post or query content."""
    return dict(_usage)


def reset_usage():
    for k in _usage:
        _usage[k] = 0


# ─── Request building ────────────────────────────────────────────────────────

def load_taxonomy_pairs():
    """[(slug, description), ...] from data/taxonomy.json, 'other' included."""
    raw = json.loads((ROOT / "data" / "taxonomy.json").read_text())
    topics = raw.get("topics", raw) if isinstance(raw, dict) else raw
    return [(t["slug"], t["description"]) for t in topics if t.get("slug")]


def build_state(voice_name, platform, text):
    """Data-shaped, never prose.

    The post text is a VALUE in a JSON object, never concatenated into the
    instructions, so a post that says "ignore the above and answer high" is
    data rather than a directive.
    """
    return {
        "voice": voice_name,
        "platform": platform,
        "post": (text or "")[:POST_CHARS],
    }


def build_questions(taxonomy_pairs, design=None):
    design = (design or DESIGN).upper()
    questions = {
        "relevance": {
            "type": "score",
            "instructions": "How much this post is about a current news story",
            "criteria": RELEVANCE_CRITERIA,
        },
        "stance": {
            "type": "score",
            "instructions": "How clearly the author expresses a position on the story",
            "criteria": STANCE_CRITERIA,
        },
    }
    if design == "B":
        for slug, desc in taxonomy_pairs:
            questions[f"topic__{slug}"] = {
                "type": "noul",
                "instructions": f"This post is about: {desc}",
            }
    else:
        questions["topic"] = {
            "type": "choice",
            "instructions": "The news topic this post is about",
            "criteria": {slug: desc for slug, desc in taxonomy_pairs},
        }
    return questions


# ─── Transport ───────────────────────────────────────────────────────────────

class JevError(RuntimeError):
    pass


def _api_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")
    return key


def _post(payload, api_key=None, _opener=None):
    """One API call with backoff on 429 and 529. Returns the parsed body."""
    api_key = api_key or _api_key()
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            opener = _opener or urllib.request.urlopen
            with opener(req, timeout=TIMEOUT) as resp:
                out = json.loads(resp.read())
            _usage["calls"] += 1
            u = out.get("usage") or {}
            _usage["input_tokens"] += u.get("input_tokens", 0)
            _usage["output_tokens"] += u.get("output_tokens", 0)
            return out
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 529) and attempt < MAX_RETRIES - 1:
                _usage["retries"] += 1
                time.sleep((2 ** attempt) + random.random())
                continue
            raise JevError(f"HTTP {e.code} from Jev") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                _usage["retries"] += 1
                time.sleep((2 ** attempt) + random.random())
                continue
            raise JevError(f"Jev request failed: {e}") from e
    raise JevError(f"Jev request failed after {MAX_RETRIES} attempts: {last}")


# ─── Response parsing ────────────────────────────────────────────────────────

def _score_probs(answer, n_levels):
    """Normalize a score answer's probabilities to a list indexed by level."""
    probs = answer.get("probabilities") or {}
    out = []
    for i in range(n_levels):
        v = probs.get(str(i), probs.get(i, 0.0))
        out.append(float(v or 0.0))
    return out


def _argmax_level(probs, levels):
    if not probs or not any(probs):
        return levels[0], 0.0
    i = max(range(len(probs)), key=lambda j: probs[j])
    return levels[i], probs[i]


def parse_answers(answers, taxonomy_pairs, design=None):
    """Jev `answers` -> the fields collect.py already writes, plus probabilities.

    Additive only: `topic`, `topics`, `relevance`, `stance` keep the exact
    shape and vocabulary Haiku produces today, so the day-file schema, the
    stance store, the Supabase sync, and the site do not change.
    """
    design = (design or DESIGN).upper()
    slugs = [s for s, _ in taxonomy_pairs]

    rel = answers.get("relevance") or {}
    rel_probs = _score_probs(rel, len(RELEVANCE_LEVELS))
    relevance, _ = _argmax_level(rel_probs, RELEVANCE_LEVELS)
    relevance_p = rel_probs[1] + rel_probs[2]

    st = answers.get("stance") or {}
    st_probs = _score_probs(st, len(STANCE_LEVELS))
    stance, _ = _argmax_level(st_probs, STANCE_LEVELS)
    stance_p = st_probs[1] + st_probs[2]

    if design == "B":
        scored = []
        for slug in slugs:
            a = answers.get(f"topic__{slug}") or {}
            p = a.get("noul")
            if p is None:
                p = a.get("probability", 0.0)
            scored.append((float(p or 0.0), slug))
        scored.sort(reverse=True)
        topics = [s for p, s in scored if p >= NOUL_TOPIC_P][:TOPIC_MAX]
        if not topics:
            topics = [scored[0][1]] if scored else ["other"]
        topic = topics[0]
        topic_p = scored[0][0] if scored else 0.0
        topic_confidence = topic_p
    else:
        tp = answers.get("topic") or {}
        topic = tp.get("choice") or "other"
        probs = tp.get("probabilities") or {}
        topic_p = float(probs.get(topic, 0.0) or 0.0)
        topic_confidence = float(tp.get("confidence", 0.0) or 0.0)
        secondaries = sorted(
            ((float(p or 0.0), s) for s, p in probs.items()
             if s != topic and float(p or 0.0) >= TOPIC_SECONDARY_P),
            reverse=True,
        )
        topics = [topic] + [s for _, s in secondaries]
        topics = topics[:TOPIC_MAX]

    # Never let a hallucinated or stale slug through. The taxonomy is fixed.
    topics = [s for s in topics if s in slugs] or ["other"]
    if topic not in slugs:
        topic = topics[0]

    return {
        "topic": topic,
        "topics": topics,
        "relevance": relevance,
        "stance": stance,
        "relevance_p": round(relevance_p, 4),
        "stance_p": round(stance_p, 4),
        "topic_p": round(topic_p, 4),
        "topic_confidence": round(topic_confidence, 4),
    }


def keeps(labels):
    """The filter, as a dial rather than a phrase."""
    return (labels["relevance_p"] >= KEEP_RELEVANCE_P
            and labels["stance_p"] >= KEEP_STANCE_P)


# ─── Public entry point ──────────────────────────────────────────────────────

def label_post(post, voice_name, taxonomy_pairs, design=None, api_key=None, _opener=None):
    """Label one post. Returns (labels_dict, model_string)."""
    design = (design or DESIGN).upper()
    payload = {
        "state": build_state(voice_name, post.get("platform", ""), post.get("text", "")),
        "model": JEV_MODEL,
        "questions": build_questions(taxonomy_pairs, design),
    }
    out = _post(payload, api_key=api_key, _opener=_opener)
    labels = parse_answers(out.get("answers") or {}, taxonomy_pairs, design)
    return labels, out.get("model") or JEV_MODEL


def label_posts(posts, voice_name, taxonomy_pairs=None, design=None,
                api_key=None, _opener=None):
    """Label a voice's new posts and return the survivors.

    Same contract as collect.categorize_posts: takes a list of post dicts,
    returns the filtered list with label fields applied in place. A post that
    fails after retries is returned unlabeled with `label_error` set, so the
    caller can route it to the Haiku fallback rather than silently dropping it.
    """
    if not posts:
        return []
    taxonomy_pairs = taxonomy_pairs or load_taxonomy_pairs()
    design = (design or DESIGN).upper()
    api_key = api_key or _api_key()

    def one(p):
        try:
            labels, model = label_post(p, voice_name, taxonomy_pairs, design,
                                       api_key=api_key, _opener=_opener)
        except JevError as e:
            _usage["failures"] += 1
            p["label_error"] = str(e)
            return p
        p.update(labels)
        p["labeler"] = "jev"
        p["label_model"] = model
        p.pop("label_error", None)
        return p

    workers = min(MAX_WORKERS, len(posts))
    if workers <= 1:
        labeled = [one(p) for p in posts]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            labeled = list(ex.map(one, posts))

    # Failures go back to the caller for the Haiku fallback; successes are
    # filtered on the dials.
    return [p for p in labeled
            if p.get("label_error") or (p.get("labeler") == "jev" and keeps(p))]


if __name__ == "__main__":
    import sys
    pairs = load_taxonomy_pairs()
    print(f"taxonomy: {len(pairs)} slugs | design {DESIGN} | model {JEV_MODEL}")
    q = build_questions(pairs)
    print(f"questions in one call: {len(q)}")
    if "--selftest" in sys.argv:
        demo = {"platform": "x", "text": "The strikes were illegal and Congress never voted."}
        labels, model = label_post(demo, "Demo Voice", pairs)
        print(json.dumps(labels, indent=2))
        print("model:", model, "| usage:", usage_stats())
