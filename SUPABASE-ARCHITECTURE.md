# Perspectives Database Architecture

**For:** Brijesh + Jack
**Date:** 2026-03-20
**Status:** Ready for review

---

## Schema Overview

```
voices (257 rows, static)
  |
  +-- posts (5000/day, partitioned monthly, raw firehose)
  |     |
  |     +-- [topic_slug] --> topics (40-50 rows, controlled taxonomy)
  |
  +-- cluster_voices (junction, ~50/day)
        |
        +-- clusters --> stories (10/day)
        |
        +-- mv_voice_alignments (materialized, refreshed daily)

editorial_overrides, content_flags, pipeline_runs (operational)
```

**7 tables + 1 materialized view + 2 functions + 2 views.**

---

## Key Design Decisions

### 1. Raw posts separated from analytical layer

**Decision:** `posts` table stores raw collected data. `cluster_voices` stores the AI-generated analytical assignments. They are linked by `post_id` but can exist independently.

**Why:** The audit (2026-03-13) identified that clustering is non-reproducible. If we re-run Claude clustering with an improved prompt, we only replace `cluster_voices` rows, not the underlying posts. This also lets us run quality audits comparing old vs. new assignments.

**How ISideWith does it:** They separate survey responses (raw) from computed position scores (derived). Same principle.

### 2. Monthly range partitioning on posts

**Decision:** `posts` is partitioned by `collected_date` using Postgres native range partitioning, one partition per month.

**Why:**
- 5000 posts/day = ~150K/month = ~1.8M/year
- Every meaningful query filters by date range
- Partition pruning means a 30-day query only touches 1-2 partitions instead of the full table
- Old partitions (13+ months) can be detached and archived without affecting live queries
- At 1.8M rows/year, partitioning gives us 3-5 years before needing to think about sharding

**Performance estimates:**
- 30-day voice profile query (voice_id + date range): ~2-5ms (hits 1 partition, uses composite index)
- Full-text search across 30 days: ~20-50ms (2 partitions, GIN index)
- Unpartitioned at 1.8M rows: same queries would be 10-20x slower after year 1

**Supabase note:** Supabase Postgres supports native partitioning. Create future partitions with pg_cron or a migration. No pg_partman on Supabase, so use a monthly cron job:
```sql
-- Run on 1st of each month via pg_cron
SELECT cron.schedule('create-next-partition', '0 0 1 * *',
  $$ SELECT create_next_month_partition() $$
);
```

### 3. Materialized view for alignment scores, function for time-windowed alignment

**Decision:** `mv_voice_alignments` is a materialized view refreshed once after each daily pipeline run. Time-windowed alignment (30/90/365 day) is a PL/pgSQL function.

**Why:**
- The all-time alignment across 257 voices = ~33K pairs to compute. Too expensive for every page load (~200ms), but trivial as a daily batch (~1-2 seconds).
- Time-windowed alignment is needed for profile pages ("how has alignment changed?"). This only computes pairs for ONE voice, so it's fast enough live (~10-30ms).
- `REFRESH MATERIALIZED VIEW CONCURRENTLY` means zero downtime during refresh. The UNIQUE index on (voice_a, voice_b) enables this.

**GovTrack comparison:** GovTrack computes "ideology scores" and "leadership scores" from roll call votes using a batch process (DW-NOMINATE algorithm). They don't recompute on every page load. Same pattern here.

**ProPublica comparison:** ProPublica's Congress API stores individual votes and computes agreement percentages dynamically for member-to-member comparisons. Works because Congress has ~535 members and votes are structured (yea/nay). Our data is messier (natural language clusters), so the materialized approach is better.

### 4. Denormalized story_id on cluster_voices

**Decision:** `cluster_voices` has both `cluster_id` and `story_id`, even though `story_id` is derivable from `cluster_id -> clusters.story_id`.

**Why:** The two most common queries are:
1. "All voices in all clusters for story X" (story detail page)
2. "All positions for voice Y across all stories" (voice profile page)

Without denormalized `story_id`, query #2 requires joining through `clusters` to get to `stories`. With it, both queries are single-table lookups with an index scan. The storage cost is 16 bytes per row (UUID), negligible at ~50 rows/day.

### 5. Controlled taxonomy with alias mapping

**Decision:** `topics` table has a fixed set of 40-50 slugs. Legacy free-form slugs are stored in the `aliases` array for backward compatibility. The pipeline MUST map to a known slug or set `topic_slug = NULL`.

**Why:** The audit found 652 unique topic slugs for 1985 posts. The `topic-mapping.json` file already maps legacy slugs. This design enforces the taxonomy at the database level via the foreign key `posts.topic_slug REFERENCES topics(slug)`.

### 6. Full-text search via GIN indexes (not a separate search service)

**Decision:** Use Postgres `to_tsvector('english', text)` GIN indexes on posts.text and stories.headline. No Elasticsearch, no Typesense.

**Why:**
- At 1.8M posts/year, Postgres full-text search is more than adequate
- Supabase has built-in full-text search support via PostgREST
- Adding trigram indexes (`pg_trgm`) on voices.name handles fuzzy name search
- A separate search service adds operational complexity for zero benefit at this scale
- If search latency becomes an issue at 10M+ posts, add a Supabase Edge Function with cached results

### 7. RLS: public read, editorial write, service-role pipeline

**Decision:** Three access tiers:
- `anon` (public): SELECT on all content tables. Stories filtered to `is_published = TRUE`. Posts filtered to those with a topic (categorized).
- `authenticated` (editorial): SELECT on everything + INSERT on flags and overrides
- `service_role` (pipeline): bypasses RLS entirely for all writes

**Why:** This is the standard Supabase pattern. The anon key is safe to expose in the frontend. The service_role key is ONLY used server-side in the pipeline. Editorial auth uses Supabase Auth (email/password for jack@newsreel.co, nadya@newsreel.co).

**What NOT to do:** Don't create a custom auth system. Don't use API keys in the frontend. Don't give anon write access to anything.

---

## Performance Estimates for Key Queries

| Query | Pattern | Expected Latency | Why |
|-------|---------|-------------------|-----|
| Today's stories with clusters | `v_story_cards WHERE story_date = today` | 5-15ms | ~10 stories, each with 4-6 clusters, ~30 voices. Small result set, index on story_date. |
| Single story detail | `v_story_cards WHERE slug = X` | 2-5ms | Single row + nested JSON aggregation |
| Voice profile (30 days) | `get_voice_positions('ben-shapiro', 30)` | 3-8ms | Composite index on cluster_voices(voice_id, story_id), joins ~20-30 stories |
| Voice alignments (30 days) | `get_voice_alignments('tucker-carlson', 30)` | 10-30ms | Scans cluster_voices for one voice, joins all co-occurring voices |
| All-time alignment refresh | `REFRESH MATERIALIZED VIEW` | 1-3 seconds | Full table scan of cluster_voices, ~33K pair computations. Runs once/day. |
| Full-text search posts | `WHERE to_tsvector(...) @@ to_tsquery('immigration')` | 20-50ms | GIN index, scoped to 30-day partition |
| Voice directory | `SELECT * FROM voices WHERE is_active` | 1-2ms | 257 rows, fits in a single page |
| Trending topics | `v_trending_topics` | 5-10ms | 7-day window, ~50 topics, small aggregation |

---

## What This Schema Does NOT Handle (By Design)

1. **User accounts / login for end users** -- Not needed for v1. Libraries access without auth.
2. **Real-time subscriptions** -- Daily refresh is the product cadence. No need for Supabase Realtime.
3. **Engagement metrics** (likes, shares, follower counts per post) -- Would require platform API access that we don't have at scale. Out of scope.
4. **Fact-checking / truth scores** -- "We show what people say, not whether it's true."
5. **Cross-story position tracking** (e.g., "Ben Shapiro's position on immigration over 365 days") -- The `get_voice_positions` function returns cluster assignments grouped by story. A higher-level "position on topic X over time" would require a separate aggregation layer. Defer to v2.

---

## Supabase-Specific Implementation Notes

### Edge Functions to create

1. **`daily-pipeline`** -- Triggered by pg_cron at 6am ET. Calls collect, categorize, cluster, refresh alignment. Or: keep as Render cron calling Supabase postgrest directly.

2. **`search`** -- Wraps full-text search with concept expansion (the CONCEPT_MAP from the current codebase). Returns stories + voices matching a query.

### pg_cron jobs

```sql
-- Refresh alignment scores after pipeline completes (~7am ET)
SELECT cron.schedule('refresh-alignments', '0 12 * * *',  -- 12:00 UTC = 7am ET
  $$ SELECT refresh_alignments() $$
);

-- Create next month's partition on the 28th
SELECT cron.schedule('create-partition', '0 0 28 * *',
  $$ -- dynamic SQL to create next month's partition $$
);
```

### PostgREST query examples (how the frontend calls these)

```javascript
// Today's stories with clusters (homepage)
const { data } = await supabase
  .from('v_story_cards')
  .select('*')
  .eq('story_date', '2026-03-20')
  .order('heat_score', { ascending: false });

// Voice profile positions
const { data } = await supabase
  .rpc('get_voice_positions', { p_voice_id: 'ben-shapiro', p_days: 30 });

// Voice alignments
const { data } = await supabase
  .rpc('get_voice_alignments', { p_voice_id: 'tucker-carlson', p_days: 90 });

// Full-text search
const { data } = await supabase
  .from('stories')
  .select('*')
  .textSearch('headline', 'iran & conflict')
  .gte('story_date', '2026-02-20')
  .order('story_date', { ascending: false });

// Voice directory with search
const { data } = await supabase
  .from('voices')
  .select('*')
  .ilike('name', '%rogan%')
  .eq('is_active', true);
```

---

## Migration Checklist

### Phase 1: Schema + Static Data (day 1)
- [ ] Create new Supabase project
- [ ] Run SUPABASE-SCHEMA.sql via Supabase SQL editor or migration
- [ ] Migrate voices.json (257 rows)
- [ ] Migrate taxonomy.json (40-50 rows)
- [ ] Verify: `SELECT COUNT(*) FROM voices` = 257

### Phase 2: Historical Data (day 2)
- [ ] Migrate posts from data/posts/*/*.json
- [ ] Migrate stories from data/stories/*.json (15 stories)
- [ ] Migrate cluster-history.json (creates additional stories + cluster_voices)
- [ ] Run `REFRESH MATERIALIZED VIEW mv_voice_alignments`
- [ ] Verify key queries return expected data

### Phase 3: Pipeline Cutover (day 3-5)
- [ ] Modify Python pipeline to write to Supabase instead of JSON files
- [ ] Deploy as Render cron job (not Jack's Mac)
- [ ] Run parallel: JSON files + Supabase for 3 days
- [ ] Verify daily data matches between both targets
- [ ] Cut over: disable JSON writes, Supabase is source of truth

### Phase 4: Frontend (week 2+)
- [ ] Next.js app reads from Supabase postgrest
- [ ] All queries use the views and functions defined in schema
- [ ] RLS verified: anon key cannot write, service_role not exposed in frontend
