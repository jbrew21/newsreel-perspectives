-- ============================================================================
-- PERSPECTIVES MIGRATION VALIDATION SUITE
-- ============================================================================
-- Run this file AFTER the full migration (SUPABASE-MIGRATION-PLAN.sql) completes.
-- Every check is a SELECT that returns a result set. The expected result is
-- documented in a comment above each query.
--
-- Workflow:
--   1. Run all checks top to bottom in psql or the Supabase SQL editor.
--   2. Any check that returns rows when it should return 0 rows is a failure.
--   3. Any count that diverges from the expected value is a failure.
--   4. Fix the migration script, re-run the migration, then re-run this file.
--
-- The file is organized into 8 sections:
--   1.  Row counts
--   2.  Referential integrity
--   3.  Data completeness
--   4.  Partition correctness
--   5.  Function smoke tests
--   6.  View correctness
--   7.  RLS verification
--   8.  Dedup checks
-- ============================================================================


-- ============================================================================
-- SECTION 1: ROW COUNTS
-- ============================================================================
-- Each query should match the source JSON count exactly.
-- If a count is lower than expected, rows were dropped during migration.
-- If higher, the upsert ran multiple times without proper dedup.
-- ============================================================================

-- CHECK 1.1: voices
-- Expected: 257 (exact -- count of "id" keys in data/voices.json)
SELECT
    'voices' AS table_name,
    COUNT(*) AS actual_count,
    257 AS expected_count,
    COUNT(*) - 257 AS delta,
    CASE WHEN COUNT(*) = 257 THEN 'PASS' ELSE 'FAIL' END AS result
FROM voices;


-- CHECK 1.2: topics
-- Expected: 41 (exact count of slugs in data/taxonomy.json)
SELECT
    'topics' AS table_name,
    COUNT(*) AS actual_count,
    41 AS expected_count,
    COUNT(*) - 41 AS delta,
    CASE WHEN COUNT(*) = 41 THEN 'PASS' ELSE 'FAIL' END AS result
FROM topics;


-- CHECK 1.3: posts (lower bound -- at least 1 post per active voice per day migrated)
-- Expected: >= 2000 (floor; actual will depend on how many date files were migrated)
SELECT
    'posts' AS table_name,
    COUNT(*) AS actual_count,
    2000 AS floor_expected,
    CASE WHEN COUNT(*) >= 2000 THEN 'PASS' ELSE 'FAIL -- below floor' END AS result
FROM posts;


-- CHECK 1.4: stories
-- Expected: >= 10 (all story JSON files migrated)
SELECT
    'stories' AS table_name,
    COUNT(*) AS actual_count,
    10 AS floor_expected,
    CASE WHEN COUNT(*) >= 10 THEN 'PASS' ELSE 'FAIL -- below floor' END AS result
FROM stories;


-- CHECK 1.5: clusters
-- Expected: >= 40 (stories have 4-6 clusters each; floor = 10 stories * 4)
SELECT
    'clusters' AS table_name,
    COUNT(*) AS actual_count,
    40 AS floor_expected,
    CASE WHEN COUNT(*) >= 40 THEN 'PASS' ELSE 'FAIL -- below floor' END AS result
FROM clusters;


-- CHECK 1.6: cluster_voices
-- Expected: >= 100 (conservative floor; 40 clusters * avg 2.5 voices)
SELECT
    'cluster_voices' AS table_name,
    COUNT(*) AS actual_count,
    100 AS floor_expected,
    CASE WHEN COUNT(*) >= 100 THEN 'PASS' ELSE 'FAIL -- below floor' END AS result
FROM cluster_voices;


-- CHECK 1.7: Summary count sheet (run this for a quick overview)
SELECT table_name, n_live_tup AS row_estimate
FROM pg_stat_user_tables
WHERE table_name IN ('voices', 'topics', 'stories', 'clusters', 'cluster_voices')
ORDER BY table_name;


-- ============================================================================
-- SECTION 2: REFERENTIAL INTEGRITY
-- ============================================================================
-- Every FK relationship is tested. Each query should return 0 rows.
-- Rows returned = orphaned records = broken references = migration bug.
-- ============================================================================

-- CHECK 2.1: posts.voice_id -> voices.id
-- Expected: 0 rows (every post must belong to a known voice)
SELECT
    'posts.voice_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM posts p
WHERE NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = p.voice_id);


-- CHECK 2.2: posts.topic_slug -> topics.slug
-- Expected: 0 rows (any non-null topic_slug must exist in the taxonomy)
SELECT
    'posts.topic_slug orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM posts p
WHERE p.topic_slug IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM topics t WHERE t.slug = p.topic_slug);

-- Diagnostic: show the bad topic slugs if the above fails
SELECT DISTINCT p.topic_slug, COUNT(*) AS post_count
FROM posts p
WHERE p.topic_slug IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM topics t WHERE t.slug = p.topic_slug)
GROUP BY p.topic_slug
ORDER BY post_count DESC;


-- CHECK 2.3: clusters.story_id -> stories.id
-- Expected: 0 rows (every cluster must belong to a known story)
SELECT
    'clusters.story_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM clusters c
WHERE NOT EXISTS (SELECT 1 FROM stories s WHERE s.id = c.story_id);


-- CHECK 2.4: clusters.best_quote_voice_id -> voices.id
-- Expected: 0 rows (non-null best_quote_voice_id must point to a real voice)
SELECT
    'clusters.best_quote_voice_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM clusters c
WHERE c.best_quote_voice_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = c.best_quote_voice_id);


-- CHECK 2.5: cluster_voices.cluster_id -> clusters.id
-- Expected: 0 rows
SELECT
    'cluster_voices.cluster_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices cv
WHERE NOT EXISTS (SELECT 1 FROM clusters c WHERE c.id = cv.cluster_id);


-- CHECK 2.6: cluster_voices.story_id -> stories.id
-- Expected: 0 rows (denormalized story_id must also be valid)
SELECT
    'cluster_voices.story_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices cv
WHERE NOT EXISTS (SELECT 1 FROM stories s WHERE s.id = cv.story_id);


-- CHECK 2.7: cluster_voices.voice_id -> voices.id
-- Expected: 0 rows (every voice assigned to a cluster must exist in voices)
SELECT
    'cluster_voices.voice_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices cv
WHERE NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = cv.voice_id);


-- CHECK 2.8: cluster_voices.story_id matches cluster_voices.cluster_id parent story
-- Expected: 0 rows (the denormalized story_id must agree with cluster.story_id)
SELECT
    'cluster_voices denormalized story_id mismatch' AS check_name,
    COUNT(*) AS mismatch_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices cv
JOIN clusters c ON c.id = cv.cluster_id
WHERE cv.story_id != c.story_id;


-- CHECK 2.9: cluster_voices.post_id -> posts.id (nullable FK)
-- Expected: 0 rows (non-null post_id must point to a real post)
SELECT
    'cluster_voices.post_id orphans' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices cv
WHERE cv.post_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.id = cv.post_id);


-- ============================================================================
-- SECTION 3: DATA COMPLETENESS
-- ============================================================================
-- These catch silent data loss where rows migrated but field values were dropped.
-- ============================================================================

-- CHECK 3.1: Voices with NULL name (NOT NULL constraint should prevent this,
-- but validates the constraint was actually applied)
-- Expected: 0 rows
SELECT
    'voices with NULL name' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM voices
WHERE name IS NULL OR name = '';


-- CHECK 3.2: Voices with NULL bio (bio is the "lens" -- core to the product)
-- Expected: 0 rows. A voice without a bio will render broken on the profile page.
-- Note: bio column is nullable in the schema, but every voice in voices.json has a lens.
SELECT
    'voices with NULL or empty bio' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM voices
WHERE bio IS NULL OR bio = '';

-- Diagnostic: list the offending voices
SELECT id, name, bio
FROM voices
WHERE bio IS NULL OR bio = ''
ORDER BY name;


-- CHECK 3.3: Voices with no posts at all
-- Expected: 0 rows (every active voice should have at least 1 post in the DB)
-- A voice with 0 posts means the collection loop skipped that voice directory.
SELECT
    'active voices with 0 posts' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN -- some voices may have no recent activity' END AS result
FROM voices v
WHERE v.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.voice_id = v.id);

-- Diagnostic
SELECT v.id, v.name
FROM voices v
WHERE v.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.voice_id = v.id)
ORDER BY v.name;


-- CHECK 3.4: Stories with 0 clusters
-- Expected: 0 rows. A published story with no clusters will render as an empty card.
SELECT
    'published stories with 0 clusters' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM stories s
WHERE s.is_published = TRUE
  AND NOT EXISTS (SELECT 1 FROM clusters c WHERE c.story_id = s.id);

-- Diagnostic
SELECT s.id, s.slug, s.headline, s.story_date
FROM stories s
WHERE s.is_published = TRUE
  AND NOT EXISTS (SELECT 1 FROM clusters c WHERE c.story_id = s.id)
ORDER BY s.story_date DESC;


-- CHECK 3.5: Clusters with 0 voices in cluster_voices
-- Expected: 0 rows. A cluster with no voice assignments has no content to display.
SELECT
    'clusters with 0 voice assignments' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM clusters c
WHERE NOT EXISTS (SELECT 1 FROM cluster_voices cv WHERE cv.cluster_id = c.id);

-- Diagnostic
SELECT c.id, c.name, c.slug, s.headline, s.story_date
FROM clusters c
JOIN stories s ON s.id = c.story_id
WHERE NOT EXISTS (SELECT 1 FROM cluster_voices cv WHERE cv.cluster_id = c.id)
ORDER BY s.story_date DESC;


-- CHECK 3.6: stories.cluster_count matches actual cluster count
-- Expected: 0 rows (the denormalized counter must match reality)
SELECT
    'stories with stale cluster_count' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- run UPDATE to fix counters' END AS result
FROM stories s
WHERE s.cluster_count != (
    SELECT COUNT(*) FROM clusters c WHERE c.story_id = s.id
);

-- Diagnostic + fix script (run manually after review):
-- UPDATE stories s
-- SET cluster_count = (SELECT COUNT(*) FROM clusters c WHERE c.story_id = s.id);


-- CHECK 3.7: stories.voice_count matches actual distinct voice count
-- Expected: 0 rows
SELECT
    'stories with stale voice_count' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- run UPDATE to fix counters' END AS result
FROM stories s
WHERE s.voice_count != (
    SELECT COUNT(DISTINCT cv.voice_id) FROM cluster_voices cv WHERE cv.story_id = s.id
);


-- CHECK 3.8: clusters.voice_count matches actual assignment count
-- Expected: 0 rows
SELECT
    'clusters with stale voice_count' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- run UPDATE to fix counters' END AS result
FROM clusters c
WHERE c.voice_count != (
    SELECT COUNT(*) FROM cluster_voices cv WHERE cv.cluster_id = c.id
);


-- CHECK 3.9: Posts with empty text
-- Expected: 0 rows (text is NOT NULL but an empty string can still sneak through)
SELECT
    'posts with empty text' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM posts
WHERE text = '' OR text IS NULL;


-- CHECK 3.10: cluster_voices with empty quote
-- Expected: 0 rows (quote is NOT NULL in schema)
SELECT
    'cluster_voices with empty quote' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM cluster_voices
WHERE quote = '' OR quote IS NULL;


-- CHECK 3.11: Posts with impossible confidence values
-- Expected: 0 rows
SELECT
    'posts with out-of-range confidence' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM posts
WHERE confidence IS NOT NULL
  AND (confidence < 0.0 OR confidence > 1.0);


-- CHECK 3.12: Topics coverage -- every taxonomy slug has at least 1 post tagged
-- Expected: 0 rows for active topics (inactive topics may legitimately have 0 posts)
SELECT
    'active topics with 0 posts tagged' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN -- some topics may be unused so far' END AS result
FROM topics t
WHERE t.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.topic_slug = t.slug);

-- Diagnostic
SELECT t.slug, t.display_name
FROM topics t
WHERE t.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.topic_slug = t.slug)
ORDER BY t.slug;


-- ============================================================================
-- SECTION 4: PARTITION CORRECTNESS
-- ============================================================================
-- Verify that posts landed in the correct monthly child partition.
-- Postgres enforces this at write time, but this confirms partition pruning
-- is working and no post has a collected_date outside all partition bounds.
-- ============================================================================

-- CHECK 4.1: Posts per partition (inspect for sanity)
-- Expected: each active month has > 0 rows; months with no data show 0
SELECT
    c.relname AS partition_name,
    p.n_live_tup AS estimated_rows,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS size
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class parent ON parent.oid = i.inhparent
JOIN pg_stat_user_tables p ON p.relname = c.relname
WHERE parent.relname = 'posts'
ORDER BY c.relname;


-- CHECK 4.2: Each post's collected_date falls within its partition's bounds
-- Expected: 0 rows (Postgres enforces this, but proves constraint is intact)
-- This uses the information_schema to dynamically get partition boundaries.
-- Simpler version: spot-check each partition directly.
SELECT 'posts_2026_01' AS partition, COUNT(*) AS rows,
       MIN(collected_date) AS min_date, MAX(collected_date) AS max_date,
       CASE WHEN MIN(collected_date) >= '2026-01-01' AND MAX(collected_date) < '2026-02-01'
            THEN 'PASS' ELSE 'FAIL -- date outside partition bounds' END AS result
FROM posts_2026_01
UNION ALL
SELECT 'posts_2026_02', COUNT(*), MIN(collected_date), MAX(collected_date),
       CASE WHEN MIN(collected_date) >= '2026-02-01' AND MAX(collected_date) < '2026-03-01'
            THEN 'PASS' ELSE 'FAIL -- date outside partition bounds' END
FROM posts_2026_02
UNION ALL
SELECT 'posts_2026_03', COUNT(*), MIN(collected_date), MAX(collected_date),
       CASE WHEN MIN(collected_date) >= '2026-03-01' AND MAX(collected_date) < '2026-04-01'
            THEN 'PASS' ELSE 'FAIL -- date outside partition bounds' END
FROM posts_2026_03;
-- Add UNION ALL blocks for 04-12 as post volume grows into those months.


-- CHECK 4.3: Posts with collected_date before the earliest partition (would error on insert)
-- Expected: 0 rows (any pre-2026 data would have been rejected unless a partition was created)
SELECT
    'posts outside all partition bounds' AS check_name,
    COUNT(*) AS count,
    MIN(collected_date) AS earliest,
    MAX(collected_date) AS latest,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- unroutable rows exist' END AS result
FROM posts
WHERE collected_date < '2026-01-01' OR collected_date >= '2027-01-01';


-- CHECK 4.4: Distribution sanity -- no single day accounts for > 25% of all posts
-- (Would indicate a migration loop that only pulled one date file)
SELECT
    collected_date,
    COUNT(*) AS posts_on_date,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_total,
    CASE WHEN COUNT(*) > 0.25 * SUM(COUNT(*)) OVER ()
         THEN 'WARN -- this date has >25% of all posts, check migration loop'
         ELSE 'OK' END AS note
FROM posts
GROUP BY collected_date
ORDER BY posts_on_date DESC
LIMIT 10;


-- ============================================================================
-- SECTION 5: FUNCTION SMOKE TESTS
-- ============================================================================
-- These execute the two main RPC functions against known voice IDs.
-- Any ERROR means the function has a runtime bug or the data is malformed.
-- Zero rows returned when data exists is also a failure.
-- ============================================================================

-- CHECK 5.1: get_voice_positions -- primary voice profile query
-- Expected: >= 1 row for ben-shapiro if he has cluster assignments in the DB.
-- If 0 rows: either ben-shapiro has no cluster_voices rows, or the date filter
-- is too narrow (check that story_date is within last 30 days).
SELECT
    'get_voice_positions(ben-shapiro, 30)' AS function_call,
    COUNT(*) AS rows_returned,
    CASE WHEN COUNT(*) >= 1 THEN 'PASS' ELSE 'FAIL -- 0 rows; check cluster_voices and story dates' END AS result
FROM get_voice_positions('ben-shapiro', 30);

-- Full output for manual inspection:
SELECT * FROM get_voice_positions('ben-shapiro', 30);


-- CHECK 5.2: get_voice_positions -- verify all expected columns are present and non-null
-- Expected: 0 rows with any NULL in the core columns
SELECT
    'get_voice_positions NULLs in required fields' AS check_name,
    COUNT(*) AS rows_with_nulls,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM get_voice_positions('ben-shapiro', 30)
WHERE story_id IS NULL
   OR story_headline IS NULL OR story_headline = ''
   OR story_date IS NULL
   OR cluster_name IS NULL OR cluster_name = ''
   OR quote IS NULL OR quote = '';


-- CHECK 5.3: get_voice_alignments -- alignment computation
-- Expected: function executes without error. May return 0 rows if ben-shapiro
-- has fewer than p_min_stories (2) co-occurrences with any other voice in 30 days.
-- If 0 rows, try a longer window.
SELECT
    'get_voice_alignments(ben-shapiro, 30)' AS function_call,
    COUNT(*) AS rows_returned,
    CASE WHEN COUNT(*) >= 0 THEN 'PASS -- no error' ELSE 'FAIL' END AS result
FROM get_voice_alignments('ben-shapiro', 30);

-- Wider window to confirm data exists at all:
SELECT
    'get_voice_alignments(ben-shapiro, 90)' AS function_call,
    COUNT(*) AS rows_returned
FROM get_voice_alignments('ben-shapiro', 90);

-- Full output:
SELECT * FROM get_voice_alignments('ben-shapiro', 30);


-- CHECK 5.4: get_voice_alignments -- alignment_score in valid range [0,1]
-- Expected: 0 rows
SELECT
    'get_voice_alignments score out of range' AS check_name,
    COUNT(*) AS bad_rows,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM get_voice_alignments('ben-shapiro', 90)
WHERE alignment_score < 0 OR alignment_score > 1;


-- CHECK 5.5: get_voice_positions on a voice that does NOT exist
-- Expected: 0 rows, no error (function should degrade gracefully)
SELECT
    'get_voice_positions on non-existent voice' AS check_name,
    COUNT(*) AS rows_returned,
    CASE WHEN COUNT(*) = 0 THEN 'PASS -- graceful empty' ELSE 'FAIL -- unexpected rows' END AS result
FROM get_voice_positions('voice-that-does-not-exist-xyz', 30);


-- CHECK 5.6: Spot-check a second voice to confirm not voice-specific bug
SELECT * FROM get_voice_positions('tucker-carlson', 30);


-- ============================================================================
-- SECTION 6: VIEW CORRECTNESS
-- ============================================================================
-- v_story_cards and v_trending_topics are the primary read surfaces for the UI.
-- ============================================================================

-- CHECK 6.1: v_story_cards returns rows
-- Expected: >= 1 row (at least one published story exists)
SELECT
    'v_story_cards row count' AS check_name,
    COUNT(*) AS rows_returned,
    CASE WHEN COUNT(*) >= 1 THEN 'PASS' ELSE 'FAIL -- no published stories visible' END AS result
FROM v_story_cards;


-- CHECK 6.2: v_story_cards required columns are non-null
-- Expected: 0 rows (the columns the UI depends on must always be populated)
SELECT
    'v_story_cards NULLs in required fields' AS check_name,
    COUNT(*) AS rows_with_nulls,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM v_story_cards
WHERE id IS NULL
   OR slug IS NULL OR slug = ''
   OR headline IS NULL OR headline = ''
   OR story_date IS NULL;


-- CHECK 6.3: v_story_cards clusters JSON is not null and is an array
-- Expected: 0 rows with null or non-array clusters
SELECT
    'v_story_cards with null clusters JSON' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM v_story_cards
WHERE clusters IS NULL OR jsonb_typeof(clusters) != 'array';


-- CHECK 6.4: v_story_cards only shows published stories
-- Expected: 0 rows (the view has WHERE is_published = TRUE)
SELECT
    'v_story_cards contains unpublished stories' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM v_story_cards sc
JOIN stories s ON s.id = sc.id
WHERE s.is_published = FALSE;


-- CHECK 6.5: v_story_cards sort order (most recent first)
-- Expected: the first row has the highest story_date
SELECT
    story_date,
    headline,
    heat_score,
    voice_count,
    cluster_count,
    jsonb_array_length(clusters) AS clusters_in_json
FROM v_story_cards
LIMIT 5;


-- CHECK 6.6: v_trending_topics returns rows (requires stories in last 7 days)
-- Expected: >= 1 row if any published story has a story_date within 7 days of now
SELECT
    'v_trending_topics row count' AS check_name,
    COUNT(*) AS rows_returned,
    CASE WHEN COUNT(*) >= 1 THEN 'PASS'
         ELSE 'WARN -- no stories in last 7 days, or no topic_slugs populated on stories' END AS result
FROM v_trending_topics;

-- Full output:
SELECT * FROM v_trending_topics;


-- CHECK 6.7: v_trending_topics topic slugs exist in topics table
-- Expected: 0 rows (trending topics must reference real taxonomy entries)
SELECT
    'v_trending_topics unknown slugs' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM v_trending_topics vt
WHERE NOT EXISTS (SELECT 1 FROM topics t WHERE t.slug = vt.slug);


-- CHECK 6.8: v_trending_topics counts are consistent with underlying data
-- Expected: story_count and voice_count are > 0 for every returned row
SELECT
    'v_trending_topics zero-count rows' AS check_name,
    COUNT(*) AS count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM v_trending_topics
WHERE story_count = 0 OR voice_count = 0;


-- ============================================================================
-- SECTION 7: RLS VERIFICATION
-- ============================================================================
-- These tests must be run as the `anon` role to validate the policies.
-- In Supabase SQL editor: the editor runs as postgres (superuser), which
-- bypasses RLS. Use SET ROLE to simulate the anon user.
--
-- Pattern: SET ROLE anon; <query>; RESET ROLE;
-- ============================================================================

-- CHECK 7.1: anon can SELECT from voices
-- Expected: row count > 0 (public read policy exists)
SET ROLE anon;
SELECT
    'anon can read voices' AS check_name,
    COUNT(*) AS rows_visible,
    CASE WHEN COUNT(*) > 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM voices;
RESET ROLE;


-- CHECK 7.2: anon CANNOT INSERT into voices
-- Expected: ERROR "new row violates row-level security policy"
-- Run this manually and confirm it raises an error, then roll back.
BEGIN;
SET ROLE anon;
INSERT INTO voices (id, name, category)
VALUES ('rls-test-voice', 'RLS Test', 'creator');
-- If no error: FAIL -- anon insert policy is missing or misconfigured
ROLLBACK;
RESET ROLE;


-- CHECK 7.3: anon can only see published stories
-- Expected: 0 rows returned by the anon query that are unpublished
SET ROLE anon;
SELECT
    'anon sees unpublished stories' AS check_name,
    COUNT(*) AS unpublished_visible,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- RLS not filtering unpublished' END AS result
FROM stories
WHERE is_published = FALSE;
RESET ROLE;


-- CHECK 7.4: anon can only see posts with a topic_slug (categorized posts only)
-- Expected: 0 rows where topic_slug is NULL (RLS policy: USING (topic_slug IS NOT NULL))
SET ROLE anon;
SELECT
    'anon sees uncategorized posts' AS check_name,
    COUNT(*) AS uncategorized_visible,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- anon can see uncategorized posts' END AS result
FROM posts
WHERE topic_slug IS NULL;
RESET ROLE;


-- CHECK 7.5: anon CANNOT SELECT from pipeline_runs
-- Expected: 0 rows (no SELECT policy for anon on pipeline_runs)
SET ROLE anon;
SELECT
    'anon cannot read pipeline_runs' AS check_name,
    COUNT(*) AS rows_visible,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL -- pipeline_runs exposed to anon' END AS result
FROM pipeline_runs;
RESET ROLE;


-- CHECK 7.6: anon CANNOT SELECT from editorial_overrides
SET ROLE anon;
SELECT
    'anon cannot read editorial_overrides' AS check_name,
    COUNT(*) AS rows_visible,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM editorial_overrides;
RESET ROLE;


-- CHECK 7.7: anon CANNOT SELECT from content_flags
SET ROLE anon;
SELECT
    'anon cannot read content_flags' AS check_name,
    COUNT(*) AS rows_visible,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM content_flags;
RESET ROLE;


-- CHECK 7.8: Confirm RLS is enabled on all expected tables (superuser check)
-- Expected: all 9 tables show rowsecurity = true
SELECT
    relname AS table_name,
    relrowsecurity AS rls_enabled,
    CASE WHEN relrowsecurity THEN 'PASS' ELSE 'FAIL -- RLS not enabled' END AS result
FROM pg_class
WHERE relname IN (
    'voices', 'topics', 'posts', 'stories', 'clusters',
    'cluster_voices', 'content_flags', 'editorial_overrides', 'pipeline_runs'
)
ORDER BY relname;


-- ============================================================================
-- SECTION 8: DEDUP CHECKS
-- ============================================================================
-- These verify that the migration's upsert logic prevented duplicate rows.
-- ============================================================================

-- CHECK 8.1: No duplicate voice IDs
-- Expected: 0 rows (voices.id is the PRIMARY KEY, so duplicates are impossible
-- at the DB level, but this confirms the constraint exists and is working)
SELECT
    'duplicate voice IDs' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT id, COUNT(*) AS cnt
    FROM voices
    GROUP BY id
    HAVING COUNT(*) > 1
) dupes;


-- CHECK 8.2: No duplicate topic slugs
-- Expected: 0 rows
SELECT
    'duplicate topic slugs' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT slug, COUNT(*) AS cnt
    FROM topics
    GROUP BY slug
    HAVING COUNT(*) > 1
) dupes;


-- CHECK 8.3: No duplicate (cluster_id, voice_id) pairs in cluster_voices
-- Expected: 0 rows (UNIQUE(cluster_id, voice_id) enforces this at the DB level)
SELECT
    'duplicate cluster_voice pairs' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT cluster_id, voice_id, COUNT(*) AS cnt
    FROM cluster_voices
    GROUP BY cluster_id, voice_id
    HAVING COUNT(*) > 1
) dupes;

-- Diagnostic: show which pairs are duplicated
SELECT cluster_id, voice_id, COUNT(*) AS cnt
FROM cluster_voices
GROUP BY cluster_id, voice_id
HAVING COUNT(*) > 1
ORDER BY cnt DESC;


-- CHECK 8.4: No duplicate (slug, story_date) pairs in stories
-- Expected: 0 rows (UNIQUE(slug, story_date) enforces this)
SELECT
    'duplicate (slug, story_date) in stories' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT slug, story_date, COUNT(*) AS cnt
    FROM stories
    GROUP BY slug, story_date
    HAVING COUNT(*) > 1
) dupes;


-- CHECK 8.5: No duplicate posts by (voice_id, platform, external_id)
-- Expected: 0 rows (partial unique index idx_posts_dedup enforces this for
-- non-null external_ids; null external_ids are excluded from the index)
SELECT
    'duplicate posts by (voice_id, platform, external_id)' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM (
    SELECT voice_id, platform, external_id, COUNT(*) AS cnt
    FROM posts
    WHERE external_id IS NOT NULL
    GROUP BY voice_id, platform, external_id
    HAVING COUNT(*) > 1
) dupes;

-- Diagnostic: most-duplicated posts
SELECT voice_id, platform, external_id, COUNT(*) AS cnt
FROM posts
WHERE external_id IS NOT NULL
GROUP BY voice_id, platform, external_id
HAVING COUNT(*) > 1
ORDER BY cnt DESC
LIMIT 20;


-- CHECK 8.6: No duplicate (voice_id, platform, text) for posts without an external_id
-- These can't be deduplicated by the unique index, so check content-level duplication.
-- Expected: ideally 0 rows; any result warrants investigation but may be acceptable
-- (two different collection runs that captured the same text before external_id was parsed).
SELECT
    'near-duplicate posts (no external_id, same text)' AS check_name,
    COUNT(*) AS duplicate_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN -- investigate manually' END AS result
FROM (
    SELECT voice_id, platform, text, COUNT(*) AS cnt
    FROM posts
    WHERE external_id IS NULL
    GROUP BY voice_id, platform, text
    HAVING COUNT(*) > 1
) dupes;


-- ============================================================================
-- SECTION 9: MATERIALIZED VIEW & ALIGNMENT INTEGRITY
-- ============================================================================

-- CHECK 9.1: mv_voice_alignments was refreshed (has rows)
-- Expected: >= 1 row (needs at least one voice pair with 3+ co-occurring stories)
SELECT
    'mv_voice_alignments populated' AS check_name,
    COUNT(*) AS row_count,
    CASE WHEN COUNT(*) >= 1 THEN 'PASS'
         ELSE 'FAIL -- run: SELECT refresh_alignments();' END AS result
FROM mv_voice_alignments;


-- CHECK 9.2: alignment_score in [0, 1]
-- Expected: 0 rows
SELECT
    'mv_voice_alignments score out of range' AS check_name,
    COUNT(*) AS bad_rows,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM mv_voice_alignments
WHERE alignment_score < 0 OR alignment_score > 1;


-- CHECK 9.3: No self-pairs (voice_a = voice_b)
-- Expected: 0 rows (the WHERE voice_id < other_voice_id in the MV prevents this)
SELECT
    'mv_voice_alignments self-pairs' AS check_name,
    COUNT(*) AS self_pair_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM mv_voice_alignments
WHERE voice_a = voice_b;


-- CHECK 9.4: All voice_a and voice_b values exist in voices
-- Expected: 0 rows
SELECT
    'mv_voice_alignments unknown voices' AS check_name,
    COUNT(*) AS orphan_count,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM mv_voice_alignments mva
WHERE NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = mva.voice_a)
   OR NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = mva.voice_b);


-- CHECK 9.5: shared_clusters <= total_stories (logical constraint)
-- Expected: 0 rows
SELECT
    'mv_voice_alignments shared > total' AS check_name,
    COUNT(*) AS bad_rows,
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END AS result
FROM mv_voice_alignments
WHERE shared_clusters > total_stories;


-- ============================================================================
-- SECTION 10: FINAL SUMMARY SCORECARD
-- ============================================================================
-- Run this last. It collects pass/fail from each key check into one result set.
-- Pipe to a spreadsheet or CI log for a quick migration sign-off.
-- ============================================================================

WITH checks AS (
    SELECT 'voices row count' AS check_name,
           CASE WHEN (SELECT COUNT(*) FROM voices) = 257 THEN 'PASS' ELSE 'FAIL' END AS result
    UNION ALL
    SELECT 'topics row count',
           CASE WHEN (SELECT COUNT(*) FROM topics) = 41 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'posts floor count',
           CASE WHEN (SELECT COUNT(*) FROM posts) >= 2000 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'stories floor count',
           CASE WHEN (SELECT COUNT(*) FROM stories) >= 10 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'clusters floor count',
           CASE WHEN (SELECT COUNT(*) FROM clusters) >= 40 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'cluster_voices floor count',
           CASE WHEN (SELECT COUNT(*) FROM cluster_voices) >= 100 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'posts.voice_id referential integrity',
           CASE WHEN NOT EXISTS (SELECT 1 FROM posts p WHERE NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = p.voice_id)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'posts.topic_slug referential integrity',
           CASE WHEN NOT EXISTS (SELECT 1 FROM posts p WHERE p.topic_slug IS NOT NULL AND NOT EXISTS (SELECT 1 FROM topics t WHERE t.slug = p.topic_slug)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'cluster_voices.voice_id referential integrity',
           CASE WHEN NOT EXISTS (SELECT 1 FROM cluster_voices cv WHERE NOT EXISTS (SELECT 1 FROM voices v WHERE v.id = cv.voice_id)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'cluster_voices.story_id referential integrity',
           CASE WHEN NOT EXISTS (SELECT 1 FROM cluster_voices cv WHERE NOT EXISTS (SELECT 1 FROM stories s WHERE s.id = cv.story_id)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'cluster_voices denormalized story_id consistency',
           CASE WHEN NOT EXISTS (SELECT 1 FROM cluster_voices cv JOIN clusters c ON c.id = cv.cluster_id WHERE cv.story_id != c.story_id) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'voices with NULL bio',
           CASE WHEN NOT EXISTS (SELECT 1 FROM voices WHERE bio IS NULL OR bio = '') THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'published stories with 0 clusters',
           CASE WHEN NOT EXISTS (SELECT 1 FROM stories s WHERE s.is_published AND NOT EXISTS (SELECT 1 FROM clusters c WHERE c.story_id = s.id)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'clusters with 0 voices',
           CASE WHEN NOT EXISTS (SELECT 1 FROM clusters c WHERE NOT EXISTS (SELECT 1 FROM cluster_voices cv WHERE cv.cluster_id = c.id)) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'no duplicate cluster_voice pairs',
           CASE WHEN NOT EXISTS (SELECT 1 FROM cluster_voices GROUP BY cluster_id, voice_id HAVING COUNT(*) > 1) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'no duplicate post external_ids',
           CASE WHEN NOT EXISTS (SELECT 1 FROM posts WHERE external_id IS NOT NULL GROUP BY voice_id, platform, external_id HAVING COUNT(*) > 1) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'no duplicate (slug, story_date) in stories',
           CASE WHEN NOT EXISTS (SELECT 1 FROM stories GROUP BY slug, story_date HAVING COUNT(*) > 1) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'posts inside partition bounds',
           CASE WHEN NOT EXISTS (SELECT 1 FROM posts WHERE collected_date < '2026-01-01' OR collected_date >= '2027-01-01') THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'mv_voice_alignments populated',
           CASE WHEN (SELECT COUNT(*) FROM mv_voice_alignments) >= 1 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'mv_voice_alignments score range',
           CASE WHEN NOT EXISTS (SELECT 1 FROM mv_voice_alignments WHERE alignment_score < 0 OR alignment_score > 1) THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'v_story_cards returns rows',
           CASE WHEN (SELECT COUNT(*) FROM v_story_cards) >= 1 THEN 'PASS' ELSE 'FAIL' END
    UNION ALL
    SELECT 'v_trending_topics returns rows',
           CASE WHEN (SELECT COUNT(*) FROM v_trending_topics) >= 1 THEN 'PASS' ELSE 'WARN' END
)
SELECT
    check_name,
    result,
    ROW_NUMBER() OVER () AS check_number
FROM checks
ORDER BY check_number;

-- Final pass rate:
SELECT
    COUNT(*) FILTER (WHERE result = 'PASS') AS passed,
    COUNT(*) FILTER (WHERE result = 'FAIL') AS failed,
    COUNT(*) FILTER (WHERE result = 'WARN') AS warnings,
    COUNT(*) AS total,
    ROUND(100.0 * COUNT(*) FILTER (WHERE result = 'PASS') / COUNT(*), 1) AS pass_pct
FROM (
    -- (paste the full CTE above here, or run separately)
    SELECT 'placeholder' AS check_name, 'PASS' AS result
) s;
