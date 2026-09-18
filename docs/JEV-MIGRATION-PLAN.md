# Perspectives: switch post labeling from Haiku to TypeSafe Jev

**Date:** 2026-09-18
**Status:** PLAN, approved in principle by Jack ("plan switching from haiku to jev, for perspectives"). Nothing built. Stage 0 (key) is Jack's.
**Sibling docs:** `PERSPECTIVES-DATA-FLOW.md` (ideal shape), `EXA-CONNECT-PLAN.md` (the API this feeds).
**Memory:** `typesafe-jev-access` in the Newsreel_OS project memory.

One sentence: Jev labels every new post (topics, relevance, stance) as typed answers with probabilities; Haiku keeps only the one job Jev cannot do, the 4-8 word position summary, and only for posts that survive the filter.

## 1. What runs today (verified in code, Sep 18)

`scripts/collect.py`, Phase 3, every pipeline run (4-5 runs a day, `.github/workflows/daily-pipeline.yml`):

1. For each voice, `load_categorized_cache()` reads yesterday's and today's day file and remembers labels for posts that were **kept**. Dropped posts are not remembered, so a "no stance" post that sits in a feed's top 20 is sent to the model again on every run until it scrolls off. This is where much of the token volume comes from.
2. `split_cached_posts()` sends only uncached posts to the model.
3. `categorize_posts_batch()` builds one prompt per voice (`_build_categorization_prompt`: the 41-slug taxonomy with descriptions, the rules, the posts at 300 chars each) and submits one Message Batch; any voice the batch misses falls back to the sequential `categorize_posts()`.
4. `_apply_categorization()` parses the JSON array (defensively; malformed elements have crashed whole runs before), applies `topics` (1-3), `relevance` (high/medium/low), `stance` (strong/lean/neutral), `summary` (text), and keeps only relevance in {high, medium} AND stance in {strong, lean}.
5. `log_usage()` writes `data/usage-log.json`. Its price table is stale ($0.80/$4.00; Haiku 4.5 list is $1.00/$5.00) and ignores the 50% batch discount.

Measured (usage log, Sep 15-17): ~313 model calls, ~680K input tokens, ~104K output tokens per run. Day file Sep 17: 18,316 kept posts across 324 voices; ~17,900 of those are cache hits per run, so ~400-450 new kept posts per run. New-to-model posts per run including the ones that get dropped: several thousand (from the token math; not logged today).

| | Today (Haiku 4.5) |
|---|---|
| Cost per run | $0.60 (batch path) to $1.20 (sequential fallback) |
| Cost per month at 5 runs/day | ~$90-180 |
| Categorization wall time | batch poll, minutes to tens of minutes inside the 90-min job |
| Output | labels only, no confidence; JSON parsed by hand |

Other Haiku call sites in the repo, NOT in scope for this migration: `lookup.py` (search-time topic match, cluster naming, Q&A), `discover_bluesky.py`, `backfill_summaries.py`, `search.py`. `stories.py` runs Sonnet 5 for story writing (text; stays).

## 2. What Jev is, in the terms that matter here

- `POST https://api.typesafe.ai/v1/systemone`, Bearer key. Python SDK `pip install typesafe-sdk`; `TypeSafeClient()` reads `TYPESAFE_API_KEY` from the environment and calls `jev-latest` by default.
- Three question types over one `state`: **Choice** (one of up to 255 options; returns `choice`, per-option `probabilities`, `confidence`), **Score** (2-10 ordered levels; returns fractional `score`, `probabilities`, `confidence`), **Noul** (yes/no as a 0-1 probability). Every question in a call is scored independently against the state; batching questions does not change answers.
- No text output. Ever.
- Limits: 64K tokens per call; 32K for state plus the longest question. 70-500 ms typical.
- Price: $0.042 per million input tokens, output free.
- Versioning: `jev-latest` currently points to `jev-1.13.0`. The docs say an alias moves when a release ships, so **we pin `jev-1.13.0`** and store the model id on every label.
- Retries: `typesafe_sdk.RetryPolicy(max_retries, backoff_initial=0.5, backoff_max=5.0, http_statuses={408, 429, 5xx}, respect_retry_after=True, timeout=30.0)`.
- Rate limits: undocumented (429 and 529 exist). Early access. No SLA.
- Data: docs state a commitment not to train on user data; ZDR is offered to enterprise customers (privacy@typesafe.ai). Our inputs are public posts by public figures, so this is acceptable; confirm the DPA before any contributor or licensed text ever goes through it (not in scope).
- Known weaknesses (their "jaggedness" page for 1.13): reads criteria literally (negations and implied conditions at face value), cannot count or compare dates, accuracy falls with irrelevant context, injected instructions inside the state can sway it, contradictory criteria hurt, P(noul) is not guaranteed to equal 1 - P(not noul). Nothing published on sarcasm, political text, or non-English text, so we measure those ourselves.

## 3. Target design

### 3.1 One Jev call per new post

State is data-shaped, never prose, so a tweet cannot smuggle instructions into the question:

```python
state = {"voice": voice_name, "platform": platform, "post": text[:300]}
```

Questions (criteria are positive statements, no negations, per the jaggedness notes):

```python
questions = {
  "relevance": Score(
    instructions="How much this post is about a current news story",
    criteria=[
      "Personal, promotional, or entertainment content with no news story",   # -> low
      "Tangentially related to a current news story",                          # -> medium
      "Clearly about a specific current news story",                           # -> high
    ]),
  "stance": Score(
    instructions="How clearly the author expresses a position on the story",
    criteria=[
      "Purely informational: a summary, both-sides reporting, or a link with no view",  # -> neutral
      "A position is implied by framing, word choice, or tone",                       # -> lean  (Sep 18: dropped "but is not stated" — negation, against this section's own rule)
      "A clear opinion, argument, criticism, praise, or call to action",                # -> strong
    ]),
  "topic": Choice(
    instructions="The news topic this post is about",
    criteria={slug: description for slug, description in taxonomy}),   # 41 options, from data/taxonomy.json
}
```

Mapping back to today's fields (so every downstream consumer is untouched):

- `relevance` = argmax of the Score probabilities -> low/medium/high. Store `relevance_p` = P(medium) + P(high).
- `stance` = argmax -> neutral/lean/strong. Store `stance_p` = P(lean) + P(strong).
- `topic` = `choice`. `topics` = `choice` plus any other option with probability >= `TOPIC_SECONDARY_P` (start 0.20), capped at 3, best first. Store `topic_p` = P(choice) and `topic_confidence`.
- New fields on every post: `labeler` ("jev" | "haiku"), `label_model` ("jev-1.13.0" | "claude-haiku-4-5-20251001"), the three probabilities above. Additive only; the day-file schema, stance store, Supabase sync, and site do not change.

Filter becomes a dial instead of a phrase: keep if `relevance_p >= KEEP_RELEVANCE_P` and `stance_p >= KEEP_STANCE_P`. Both start at 0.50, which reproduces today's argmax rule exactly; shadow mode tunes them. This is Jack's step 2 ("AI drops posts that take no stand") with a knob, and `stance_p` is the confidence field the AI-licensing positions layer wants.

Design B, tested alongside A in shadow mode: replace the single `topic` Choice with 41 Nouls ("This post is about: <description>"), take every topic with P >= 0.5, cap 3, primary = max. Semantically the right shape for multi-label; roughly the same token cost. Pick A or B on agreement with Haiku and the blind review, not on theory. Design C, only if both disappoint: a two-level hierarchy (about 8 parent groups, then children), per TypeSafe's hierarchical-classification cookbook.

### 3.2 Haiku keeps the summary, and only for survivors

After Jev, the kept posts per voice go to Haiku in one short prompt: the posts and "summarize the author's position in 4-8 neutral words." No taxonomy block, no rules about slugs, no relevance or stance instructions. Message Batches as today, sequential fallback as today. Cache hits keep their stored summary.

### 3.3 Concurrency, retries, fallback

- Thread pool, `JEV_CONCURRENCY` = 8 to start. 3,000 posts at ~200 ms is ~75 s.
- `RetryPolicy(max_retries=3, respect_retry_after=True, timeout=20.0)`.
- If a post still fails after retries, that voice's uncached posts go through today's Haiku path (`categorize_posts` with the full prompt), and the run continues. The usage log records `jev_calls`, `jev_input_tokens`, `jev_failures`, `haiku_fallback_voices`.
- `check_collection_health.py` gets a new floor: kept-new-posts per run under a committed minimum fails the build, the same way the X floor catches a dead collector. A labeler that silently drops everything must not ship an empty day.

### 3.4 Dropped-URL cache (small, and worth doing in the same change)

`load_categorized_cache()` learns to remember dropped URLs too (a `data/dropped/<voice>.json` set with a 3-day window). A post the model rejected yesterday is not sent again today. This cuts new-to-model posts per run from thousands to roughly the ~450 that are actually new, on either labeler.

### 3.5 What changes and what does not

| Changes | Does not change |
|---|---|
| `collect.py` Phase 3 (a `LABELER` switch, see §4) | Collectors, `x_guest.py`, feeds, taxonomy |
| New `scripts/label_jev.py`, `scripts/shadow_compare.py` | Day-file schema (fields added, none removed) |
| `requirements.txt` (+ `typesafe-sdk`) | `build_stances.py`, Supabase sync, `serve.py`, the site |
| `log_usage()` prices and new counters | `stories.py` (Sonnet 5), `lookup.py`, `enrich_transcripts.py` |
| `check_collection_health.py` (kept-posts floor) | The 4-5 daily cron schedule |
| Workflow: `TYPESAFE_API_KEY` secret + `LABELER` variable | Render deploy step |

**Schedule correction (Sep 18):** the row above used to say the "4-5 daily cron schedule" does not change. It changed the same day, on Jack's call: **two runs, 10:17 and 22:17 UTC (6:17 AM / 6:17 PM ET)**. Halving the runs halves the labeling bill again on top of this migration, and coverage was verified first (the collector fetches 20 posts per feed; no voice-day in 15,922 rows exceeds 40 posts, so a 12-hour gap fits). What it gives up is the dropped-run backstops; the corpus self-heals on the next run, only site freshness suffers.

**Stage 1 status: BUILT Sep 18.** `scripts/label_jev.py` (both designs, thresholds as module constants, backoff on 429/529, failures returned for the Haiku fallback rather than dropped), `tests/test_label_jev.py` (28 tests, suite green at 233), `scripts/shadow_compare.py` (offline, `--dry-run` costs nothing). Measured on a dry run: 19,168 unique Haiku-kept posts in the last 3 days; a 400-post sample across both designs costs about **$0.06**. Stage 1 cannot run for real until Stage 0 (the key) is done.

**One spec defect found while building:** this document's own stance criterion for "lean" read "...but is not stated", which is a negation, against the no-negations rule stated three lines above it. A unit test caught it. The negative clause was dropped rather than reworded, because the ordering against level 2 already carries the implicit/explicit distinction.

## 4. Stages and gates

**Stage 0 - key (Jack, 10 min).** The key pasted into chat on Sep 18 is compromised by definition. Rotate it at console.typesafe.ai. Put the new one in the gitignored Newsreel_OS root `.env` as `TYPESAFE_API_KEY`. Never paste it again; the SDK reads it from the environment.

**Stage 1 - offline shadow (Claude, ~3 h, touches nothing live).**
- `scripts/label_jev.py`: the questions above, mapping, thresholds as module constants, retry, concurrency, a `label_posts(posts) -> posts` function with the same contract as `_apply_categorization`.
- `scripts/shadow_compare.py --days 3 --design A|B|both --sample N`: reads the last 3 day files (~19K unique kept posts, text at 300 chars, Haiku labels attached), runs Jev, writes `data/shadow/offline-<date>.jsonl` and prints: primary-topic agreement, topic-set Jaccard, relevance exact and adjacent, stance exact and adjacent, **would-Jev-keep rate** (the false-drop rate against Haiku's kept set), confidence histograms, per-platform breakdown, 429 count, p50/p95 latency, tokens and dollars. Estimated run: ~40M tokens across both designs, under $2, ~15-20 min at 8 workers.
- Unit tests in `tests/test_label_jev.py` (mock client): mapping, thresholds, secondary-topic cap, fallback trigger, model pin. Suite stays green (`python3 -m unittest`).
- Limit of this stage: day files hold only kept posts, so it measures false drops, not false keeps.

**Gate 1 (Jack + Claude, ~1 h).** Blind review of 100 disagreements (post, label A, label B, no attribution). Pick design A or B. Pass if: false-drop rate <= 10% or Jev wins the review on those; primary-topic agreement >= 85%; stance adjacent >= 95%. Fail = stop, write up why, nothing was changed.

**Stage 2 - live shadow (Claude ~3 h; Jack adds the secret and variable).**
- `LABELER` environment variable in `collect.py`: `haiku` (default; today's code path, byte for byte), `shadow` (Haiku decides; Jev also runs on the same new posts and both label sets, including the posts Haiku dropped, go to `data/shadow/<date>-<HHMM>.jsonl`), `jev` (Jev decides; Haiku summaries on survivors; Haiku fallback per voice).
- Dropped-URL cache (§3.4), `log_usage()` price fix and new counters, health floor, `typesafe-sdk` in requirements.
- Workflow: `TYPESAFE_API_KEY` from secrets, `LABELER` from a repository variable so cutover and rollback are a settings change, not a commit.
- Push needs Jack's go (the Action runs from main). Run with `LABELER=shadow` for 3-5 days (15-25 runs).

**Gate 2.** Same metrics as Gate 1 in both directions (now including false keeps), plus: no run exceeded its time budget, 429s handled by backoff without fallback storms, shadow files committed cleanly. Thresholds `KEEP_RELEVANCE_P`, `KEEP_STANCE_P`, `TOPIC_SECONDARY_P` frozen from the shadow data.

**Stage 3 - cutover.** Set `LABELER=jev`. Watch 7 days: usage log, health check, a daily glance at "what got kept" on the site. Rollback is `LABELER=haiku`; a mixed history is fine because every post says who labeled it.

**Stage 4 - close-out.** Relabel the stance store once with the pinned model so `data/stances/` carries one `label_model` (about 44K positions, ~50M tokens, ~$2). Record the version and thresholds in this doc. Any future `jev-x.y.z` bump re-runs `shadow_compare.py` first.

Effort: Claude ~10 h across Stages 1-3; Jack ~1.5 h (key, two settings, two gate reviews). Brijesh: nothing (no Supabase, no Render changes). Calendar: about two weeks, most of it waiting on shadow runs.

## 5. Expected result

| | Today | After cutover |
|---|---|---|
| Model calls per run | ~313 Haiku (one per voice) | ~450-3,000 Jev (one per new post; ~450 once the dropped-URL cache is in) + ~313 short Haiku summary calls |
| Input tokens per run | ~680K Haiku | ~0.5-3.5M Jev + ~100-150K Haiku |
| Cost per run | $0.60-1.20 | ~$0.02-0.15 Jev + ~$0.08-0.15 Haiku |
| Cost per month | ~$90-180 | ~$15-40 |
| Categorization wall time | minutes to tens of minutes | ~1-2 min |
| Parse failures | possible; hardened by hand | none (typed) |
| Per-label confidence | none | stored on every post; the filter is a threshold, not a phrase |
| Relabel the whole corpus | ~$50-100 and an afternoon | ~$2 and ~10 min |

The savings are real but not the reason. The reasons are: the run gets faster inside a 90-minute job that already has to fit collection plus stories; a whole class of failure (malformed model JSON) goes away; and every position gains a calibrated confidence the AI-licensing door can expose and the site can sort by.

## 6. Risks and the answer to each

| Risk | Answer |
|---|---|
| Early access: undocumented rate limits, no SLA, could change or vanish | Concurrency knob, RetryPolicy, per-voice Haiku fallback, `LABELER` rollback in one setting, health floor. The Haiku path never leaves the codebase. |
| Literal reading | Criteria are positive statements; no "not", no double negatives; "when in doubt include it" becomes a threshold. |
| Political text, sarcasm, bias: unmeasured by TypeSafe | Shadow mode measures agreement per platform; the Gate 1 review is blind; check whether disagreements skew by a voice's side. |
| Injected instructions inside a post | State is JSON with the text as a value; instructions live in the questions. Stakes are one mislabeled post. |
| Version drift | Pin `jev-1.13.0`; `label_model` on every post; re-run shadow before any bump. |
| Two models, two bills | Both logged per run in `usage-log.json` with corrected prices. |
| Data terms | Public posts only. No contributor or licensed text through Jev until the DPA is read. |

## 7. Follow-ons (not this migration)

**Prompt caching, if this migration stalls.** There is no `cache_control` anywhere in `scripts/` (verified Sep 18). Today every one of the ~313 per-voice calls re-sends the taxonomy and the rule block in full. Section 3.2 removes that block from the Haiku call entirely, which is the better fix, but it only lands if Jev ships. If Jev is delayed, cache the stable prefix on the existing Haiku prompt instead: restructure `_build_categorization_prompt` so instructions, taxonomy and rules sit in a cached system prompt and only the posts vary. No quality risk, no new vendor.

**Confirm the Batch API is actually engaging.** `categorize_posts_batch` and the sequential fallback both increment `_usage_stats['claude_calls']`, so the usage log cannot tell you which path ran, and the two differ by 50% on price (`$0.60` vs `$1.20` a run). Log the path taken, then read one GitHub Action run. This is worth more than the model swap if it has been silently falling back.

**Fix the price table first, whatever else happens.** `log_usage()` at `collect.py:1435` prices input at $0.80/M and output at $4.00/M, under a comment citing Sonnet. Haiku 4.5 list is $1.00/M and $5.00/M. Every cost figure in `data/usage-log.json` is about 25% low, which means no before/after measurement of this migration is trustworthy until it is corrected.


- `lookup.py` `_match_story_to_topics()`: search-time query-to-topic mapping as a Jev Choice with the same 41 criteria. Sub-second instead of a 10 s-timeout Haiku call; the result cache keeps working.
- `assign_argument_clusters()`: Haiku still names the 4-6 clusters (text); Jev assigns the 60 voices with one Choice each. Removes the max_tokens overflow that once shipped no clusters at all.
- `discover_bluesky.py` handle matching ("same person?") as a Noul.
- Exa / one-door API: expose `stance_p` and `topic_p` as filterable fields (see `EXA-CONNECT-PLAN.md`).
