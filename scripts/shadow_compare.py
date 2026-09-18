#!/usr/bin/env python3
"""
Offline shadow: score Jev's labels against the Haiku labels already sitting in
the day files. Touches nothing live and writes nothing outside data/shadow/.

  python3 scripts/shadow_compare.py --days 3 --design both --sample 400
  python3 scripts/shadow_compare.py --dry-run          # cost + plan, no calls

What it can and cannot measure (Stage 1 limit, from the migration plan): day
files hold only the posts Haiku KEPT. So this measures false drops, where Jev
would discard something Haiku kept. It cannot measure false keeps, because the
posts Haiku threw away were never written down. Stage 2 (live shadow) is what
closes that gap.

Output:
  data/shadow/summary-<date>.json       the numbers
  data/shadow/disagreements-<date>.json blind review set for Gate 1: post,
                                        label A, label B, no attribution
"""

import argparse
import collections
import datetime
import glob
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import label_jev  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "shadow"

# Jev list price, Sep 2026: $0.042 per 1M input tokens, output free.
JEV_INPUT_PER_M = 0.042


def load_haiku_labeled(days):
    """Unique kept posts from the last N day files, newest label wins."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    seen = {}
    for f in sorted(glob.glob(str(ROOT / "data" / "posts" / "*" / "*.json"))):
        stem = Path(f).stem
        try:
            if datetime.date.fromisoformat(stem) < cutoff:
                continue
        except ValueError:
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        voice = d.get("voiceId")
        vname = d.get("voiceName") or voice
        for p in d.get("posts", []):
            url = p.get("sourceUrl")
            if not url or not p.get("text"):
                continue
            seen[(voice, url)] = {
                "voiceId": voice,
                "voiceName": vname,
                "platform": p.get("platform", ""),
                "text": p.get("text", ""),
                "sourceUrl": url,
                "haiku": {
                    "topic": p.get("topic"),
                    "topics": p.get("topics") or ([p["topic"]] if p.get("topic") else []),
                    "relevance": p.get("relevance"),
                    "stance": p.get("stance"),
                },
            }
    return list(seen.values())


def estimate_cost(n_posts, design, taxonomy_pairs):
    """Rough input-token estimate. 4 chars per token, same as log_usage()."""
    qs = label_jev.build_questions(taxonomy_pairs, design)
    q_chars = len(json.dumps(qs))
    state_chars = 120 + label_jev.POST_CHARS
    per_post = (q_chars + state_chars) / 4
    total = per_post * n_posts
    return total, total / 1_000_000 * JEV_INPUT_PER_M


def score(rows, design):
    """Agreement between Jev and Haiku on the rows that got labeled."""
    m = collections.Counter()
    disagreements = []
    for r in rows:
        jev = r.get(f"jev_{design}")
        if not jev:
            m["errored"] += 1
            continue
        m["scored"] += 1
        h = r["haiku"]

        if jev["topic"] == h["topic"]:
            m["topic_exact"] += 1
        if jev["topic"] in (h["topics"] or []) or h["topic"] in jev["topics"]:
            m["topic_overlap"] += 1
        if jev["relevance"] == h["relevance"]:
            m["relevance_exact"] += 1
        if jev["stance"] == h["stance"]:
            m["stance_exact"] += 1

        # Haiku kept every row here, so a Jev drop is a false drop by definition.
        if not label_jev.keeps(jev):
            m["false_drop"] += 1

        if jev["topic"] != h["topic"] or jev["stance"] != h["stance"]:
            disagreements.append({
                "post": r["text"][:300],
                "platform": r["platform"],
                "label_a": {"topic": h["topic"], "relevance": h["relevance"],
                            "stance": h["stance"]},
                "label_b": {"topic": jev["topic"], "relevance": jev["relevance"],
                            "stance": jev["stance"],
                            "relevance_p": jev["relevance_p"],
                            "stance_p": jev["stance_p"]},
            })
    return m, disagreements


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--design", default="both", choices=["A", "B", "both"])
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pairs = label_jev.load_taxonomy_pairs()
    rows = load_haiku_labeled(args.days)
    print(f"{len(rows):,} unique Haiku-kept posts in the last {args.days} days "
          f"({len(pairs)} taxonomy slugs)")

    random.seed(args.seed)
    if args.sample and len(rows) > args.sample:
        rows = random.sample(rows, args.sample)
    print(f"sampling {len(rows):,} (seed {args.seed}, so reruns compare like for like)")

    designs = ["A", "B"] if args.design == "both" else [args.design]

    total_cost = 0.0
    for d in designs:
        toks, cost = estimate_cost(len(rows), d, pairs)
        total_cost += cost
        print(f"  design {d}: ~{toks/1e6:.2f}M input tokens, ~${cost:.2f}")
    print(f"  total: ~${total_cost:.2f}")

    if args.dry_run:
        print("\ndry run, no API calls made")
        return 0

    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        print("\nTYPESAFE_API_KEY is not set. Set it and rerun, or use --dry-run.",
              file=sys.stderr)
        return 2

    for d in designs:
        label_jev.reset_usage()
        print(f"\nlabeling with design {d} ...")
        for i, r in enumerate(rows, 1):
            try:
                labels, model = label_jev.label_post(
                    {"platform": r["platform"], "text": r["text"]},
                    r["voiceName"], pairs, d)
                r[f"jev_{d}"] = labels
                r["jev_model"] = model
            except label_jev.JevError as e:
                r[f"jev_{d}_error"] = str(e)
            if i % 50 == 0:
                print(f"  {i}/{len(rows)}")
        print(f"  usage: {label_jev.usage_stats()}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    summary = {"date": today, "days": args.days, "sampled": len(rows),
               "seed": args.seed, "designs": {}}

    for d in designs:
        m, dis = score(rows, d)
        n = m["scored"]
        summary["designs"][d] = {
            "scored": n,
            "errored": m["errored"],
            "topic_exact_pct": pct(m["topic_exact"], n),
            "topic_overlap_pct": pct(m["topic_overlap"], n),
            "relevance_exact_pct": pct(m["relevance_exact"], n),
            "stance_exact_pct": pct(m["stance_exact"], n),
            "false_drop_pct": pct(m["false_drop"], n),
            "disagreements": len(dis),
        }
        random.shuffle(dis)
        (OUT_DIR / f"disagreements-{d}-{today}.json").write_text(
            json.dumps(dis[:100], indent=2))

        s = summary["designs"][d]
        print(f"\ndesign {d}  (n={n}, errors={m['errored']})")
        print(f"  topic exact      {s['topic_exact_pct']:>5}%")
        print(f"  topic overlap    {s['topic_overlap_pct']:>5}%")
        print(f"  relevance exact  {s['relevance_exact_pct']:>5}%")
        print(f"  stance exact     {s['stance_exact_pct']:>5}%")
        print(f"  FALSE DROPS      {s['false_drop_pct']:>5}%   (gate 1 passes at <= 10%)")

    # Save the full per-row result so threshold sweeps and confusion analysis
    # cost nothing. Without this, every re-analysis means paying for the API
    # again (learned the hard way on the first 400-post run, Sep 18).
    (OUT_DIR / f"rows-{today}.json").write_text(json.dumps([
        {k: r[k] for k in r if k not in ("text",)} | {"text": r["text"][:300]}
        for r in rows], indent=2))

    (OUT_DIR / f"summary-{today}.json").write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT_DIR}/summary-{today}.json")
    print("disagreement files carry label_a / label_b with no attribution, "
          "so Gate 1 can be reviewed blind.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
