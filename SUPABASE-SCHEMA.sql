-- ============================================================================
-- NEWSREEL PERSPECTIVES -- Supabase Postgres Schema
-- ============================================================================
-- Generated: 2026-03-20
-- Context: 257 voices, 7 platforms, ~5000 posts/day, ~10 stories/day
-- Design goals:
--   1. Fast voice profile queries (all positions for voice X in last N days)
--   2. Fast story queries (all clusters + voices for one story)
--   3. Full-text + topic search
--   4. Cross-voice alignment scores (materialized)
--   5. Clean separation: raw posts vs. analytical layer (clusters)
--   6. Public read, editorial write, pipeline service-role only
-- ============================================================================

-- ── Extensions ──────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";      -- trigram indexes for fuzzy search


-- ============================================================================
-- 1. VOICES (relatively static, ~257 rows)
-- ============================================================================

CREATE TABLE voices (
    id              TEXT PRIMARY KEY,                -- 'joe-rogan' (slug)
    name            TEXT NOT NULL,
    photo_url       TEXT,
    bio             TEXT,                            -- the "lens" description
    category        TEXT NOT NULL DEFAULT 'creator', -- journalist, commentator, creator, politician, activist
    approach        TEXT,                            -- investigates, explains, argues, reports, entertains
    tags            TEXT[] DEFAULT '{}',             -- ['libertarian-leaning', 'anti-establishment']
    handles         JSONB DEFAULT '{}',              -- {x: 'joerogan', youtube: 'joerogan', ...}
    feeds           JSONB DEFAULT '{}',              -- {youtube: 'https://...', x: 'https://...'}
    followers       BIGINT DEFAULT 0,
    followers_display TEXT,                          -- '39.9M'
    platforms       TEXT[] DEFAULT '{}',             -- ['youtube', 'x', 'podcast']
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- For directory page filtering
CREATE INDEX idx_voices_category ON voices(category);
CREATE INDEX idx_voices_active ON voices(is_active) WHERE is_active = TRUE;
-- For name search
CREATE INDEX idx_voices_name_trgm ON voices USING gin(name gin_trgm_ops);


-- ============================================================================
-- 2. TOPICS (controlled taxonomy, ~40-50 rows)
-- ============================================================================

CREATE TABLE topics (
    slug            TEXT PRIMARY KEY,                -- 'iran-conflict'
    display_name    TEXT NOT NULL,                   -- 'Iran Conflict'
    description     TEXT,
    aliases         TEXT[] DEFAULT '{}',             -- legacy slugs that map here
    sort_order      INTEGER DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ============================================================================
-- 3. POSTS (raw collected data, ~5000/day, ~1.8M/year)
-- ============================================================================
-- This is the append-only firehose. Separated from the analytical layer
-- (clusters) so raw data is never lost even if clustering is re-run.
--
-- PARTITIONING STRATEGY: Range-partition by collected_date (monthly).
-- At 5000 rows/day, each monthly partition holds ~150K rows.
-- Queries almost always filter by date range, so partition pruning is
-- highly effective. Partitions older than 13 months can be detached
-- and archived to cold storage.
-- ============================================================================

CREATE TABLE posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    voice_id        TEXT NOT NULL REFERENCES voices(id),
    platform        TEXT NOT NULL,                   -- x, youtube, bluesky, tiktok, instagram, substack, podcast
    post_type       TEXT DEFAULT 'post',             -- tweet, video_title, short, article, episode, repost
    text            TEXT NOT NULL,
    quote           TEXT,                            -- best extracted quote (may differ for transcripts)
    source_url      TEXT,
    external_id     TEXT,                            -- platform-native ID for dedup
    topic_slug      TEXT REFERENCES topics(slug),    -- from controlled taxonomy
    relevance       TEXT DEFAULT 'medium',           -- high, medium, low
    confidence      REAL,                            -- 0.0-1.0 from Claude categorization
    published_at    TIMESTAMPTZ,                     -- when the voice posted it
    collected_date  DATE NOT NULL,                   -- when our pipeline collected it
    created_at      TIMESTAMPTZ DEFAULT NOW()
) PARTITION BY RANGE (collected_date);

-- Create initial partitions (monthly)
-- In production, use pg_partman or a cron to auto-create future partitions
CREATE TABLE posts_2026_01 PARTITION OF posts FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE posts_2026_02 PARTITION OF posts FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE posts_2026_03 PARTITION OF posts FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE posts_2026_04 PARTITION OF posts FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE posts_2026_05 PARTITION OF posts FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE posts_2026_06 PARTITION OF posts FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE posts_2026_07 PARTITION OF posts FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE posts_2026_08 PARTITION OF posts FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE posts_2026_09 PARTITION OF posts FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE posts_2026_10 PARTITION OF posts FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE posts_2026_11 PARTITION OF posts FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE posts_2026_12 PARTITION OF posts FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- PRIMARY query: "all posts by voice X in last N days"
-- This composite index covers the voice profile page query perfectly.
-- Partition pruning handles the date filter; the index handles voice_id within partition.
CREATE INDEX idx_posts_voice_date ON posts(voice_id, collected_date DESC);

-- SECONDARY query: "all posts on topic Y in last N days" (story detection)
CREATE INDEX idx_posts_topic_date ON posts(topic_slug, collected_date DESC);

-- DEDUP: prevent re-inserting the same post
CREATE UNIQUE INDEX idx_posts_dedup ON posts(voice_id, platform, external_id)
    WHERE external_id IS NOT NULL;

-- Full-text search on post content
CREATE INDEX idx_posts_text_search ON posts USING gin(to_tsvector('english', text));


-- ============================================================================
-- 4. STORIES (analytical output, ~10/day, ~3650/year)
-- ============================================================================

CREATE TABLE stories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT NOT NULL,                   -- 'iran-update-10-days-in-...'
    headline        TEXT NOT NULL,
    summary         TEXT,
    story_type      TEXT DEFAULT 'split',            -- split, spectrum, consensus, reaction
    source          TEXT DEFAULT 'voices',           -- 'voices' (auto-detected) or 'editorial' (CMS)
    heat_score      INTEGER DEFAULT 0,               -- derived from voice count + engagement
    voice_count     INTEGER DEFAULT 0,
    cluster_count   INTEGER DEFAULT 0,
    cover_url       TEXT,
    topic_slugs     TEXT[] DEFAULT '{}',             -- ['iran-conflict', 'economy-trade']
    story_date      DATE NOT NULL,
    is_published    BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(slug, story_date)                         -- same slug can appear on different days
);

CREATE INDEX idx_stories_date ON stories(story_date DESC);
CREATE INDEX idx_stories_published ON stories(story_date DESC, is_published) WHERE is_published = TRUE;
CREATE INDEX idx_stories_topic ON stories USING gin(topic_slugs);
-- Full-text search on headlines
CREATE INDEX idx_stories_headline_search ON stories USING gin(to_tsvector('english', headline));


-- ============================================================================
-- 5. CLUSTERS (argument groups within a story, ~40-60/day)
-- ============================================================================
-- Each story has 4-6 clusters. A cluster is NOT left/right -- it is an
-- argument position like "Skeptical of Optimistic Timelines" or
-- "Support Military Action". Voices are grouped by argument, not politics.
-- ============================================================================

CREATE TABLE clusters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id        UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,                   -- 'Support for Military Action Against Iran'
    slug            TEXT NOT NULL,                   -- 'pro-war-support'
    description     TEXT,                            -- optional longer description of the argument
    voice_count     INTEGER DEFAULT 0,
    sort_order      INTEGER DEFAULT 0,              -- display order (largest cluster first)
    -- Best representative quote for the cluster header
    best_quote_voice_id TEXT REFERENCES voices(id),
    best_quote_text TEXT,
    best_quote_platform TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_clusters_story ON clusters(story_id);


-- ============================================================================
-- 6. CLUSTER_VOICES (junction: which voice is in which cluster)
-- ============================================================================
-- This is the core analytical record: "Voice X took position Y on Story Z"
-- It is the foundation for alignment scores and voice profiles.
-- ============================================================================

CREATE TABLE cluster_voices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_id      UUID NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    story_id        UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE, -- denormalized for query speed
    voice_id        TEXT NOT NULL REFERENCES voices(id),
    quote           TEXT NOT NULL,                   -- the voice's actual quote for this position
    source_url      TEXT,
    platform        TEXT,
    post_id         UUID REFERENCES posts(id),       -- link back to the raw post
    quote_quality   INTEGER DEFAULT 5,               -- 1-10 fit score from validation pass
    confidence      REAL DEFAULT 0.7,                -- how sure Claude is about this assignment
    assigned_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(cluster_id, voice_id)                     -- one voice per cluster per story
);

-- THE KEY INDEX: "give me all of voice X's cluster assignments"
-- This powers the voice profile page: all positions across all stories
CREATE INDEX idx_cv_voice ON cluster_voices(voice_id);

-- For story detail page: all voices in all clusters for a story
CREATE INDEX idx_cv_story ON cluster_voices(story_id);

-- Composite for the 30-day voice profile query (joins with stories.story_date)
CREATE INDEX idx_cv_voice_story ON cluster_voices(voice_id, story_id);


-- ============================================================================
-- 7. VOICE ALIGNMENTS (materialized view - who agrees with whom)
-- ============================================================================
-- Alignment = how often two voices end up in the same argument cluster.
-- This is expensive to compute live (~257^2 / 2 = 33K pairs), so we
-- materialize it and refresh after each daily pipeline run.
--
-- ISideWith uses a similar approach: pairwise agreement scores computed
-- from categorical position data, refreshed periodically. GovTrack computes
-- "ideology scores" from vote patterns (analogous to our cluster assignments).
-- ProPublica's Congress API stores vote-level data and computes agreement
-- percentages on read. Our approach is closest to ISideWith.
-- ============================================================================

CREATE MATERIALIZED VIEW mv_voice_alignments AS
WITH pair_data AS (
    SELECT
        a.voice_id AS voice_a,
        b.voice_id AS voice_b,
        a.story_id,
        CASE WHEN a.cluster_id = b.cluster_id THEN 1 ELSE 0 END AS same_cluster
    FROM cluster_voices a
    JOIN cluster_voices b ON a.story_id = b.story_id
        AND a.voice_id < b.voice_id  -- avoid duplicates and self-joins
)
SELECT
    voice_a,
    voice_b,
    COUNT(*) AS total_stories,
    SUM(same_cluster) AS shared_clusters,
    ROUND(SUM(same_cluster)::NUMERIC / COUNT(*)::NUMERIC, 3) AS alignment_score
FROM pair_data
GROUP BY voice_a, voice_b
HAVING COUNT(*) >= 3  -- only show pairs that co-occur in 3+ stories
ORDER BY alignment_score DESC;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX idx_mv_align_pair ON mv_voice_alignments(voice_a, voice_b);
-- For "who aligns with voice X?"
CREATE INDEX idx_mv_align_a ON mv_voice_alignments(voice_a, alignment_score DESC);
CREATE INDEX idx_mv_align_b ON mv_voice_alignments(voice_b, alignment_score DESC);


-- ============================================================================
-- 7b. TIME-WINDOWED ALIGNMENT (function, not materialized)
-- ============================================================================
-- For "how has alignment changed over 30/90/365 days?"
-- This runs on-demand because time windows shift daily.
-- With proper indexes on cluster_voices + stories, this is fast enough
-- for single-voice lookups (< 50ms).
-- ============================================================================

CREATE OR REPLACE FUNCTION get_voice_alignments(
    p_voice_id TEXT,
    p_days INTEGER DEFAULT 30,
    p_min_stories INTEGER DEFAULT 2
)
RETURNS TABLE (
    other_voice_id TEXT,
    other_voice_name TEXT,
    other_voice_photo TEXT,
    total_stories BIGINT,
    shared_clusters BIGINT,
    alignment_score NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    WITH my_positions AS (
        SELECT cv.story_id, cv.cluster_id
        FROM cluster_voices cv
        JOIN stories s ON s.id = cv.story_id
        WHERE cv.voice_id = p_voice_id
          AND s.story_date >= CURRENT_DATE - p_days
    ),
    other_positions AS (
        SELECT cv.voice_id, cv.story_id, cv.cluster_id
        FROM cluster_voices cv
        JOIN stories s ON s.id = cv.story_id
        WHERE cv.voice_id != p_voice_id
          AND s.story_date >= CURRENT_DATE - p_days
    )
    SELECT
        op.voice_id AS other_voice_id,
        v.name AS other_voice_name,
        v.photo_url AS other_voice_photo,
        COUNT(*)::BIGINT AS total_stories,
        SUM(CASE WHEN mp.cluster_id = op.cluster_id THEN 1 ELSE 0 END)::BIGINT AS shared_clusters,
        ROUND(
            SUM(CASE WHEN mp.cluster_id = op.cluster_id THEN 1 ELSE 0 END)::NUMERIC
            / COUNT(*)::NUMERIC,
            3
        ) AS alignment_score
    FROM my_positions mp
    JOIN other_positions op ON mp.story_id = op.story_id
    JOIN voices v ON v.id = op.voice_id
    GROUP BY op.voice_id, v.name, v.photo_url
    HAVING COUNT(*) >= p_min_stories
    ORDER BY alignment_score DESC;
END;
$$ LANGUAGE plpgsql STABLE;


-- ============================================================================
-- 7c. VOICE POSITION HISTORY (function for profile page)
-- ============================================================================
-- "Where does this person stand on today's stories, and how does that
--  compare to their history?"
-- ============================================================================

CREATE OR REPLACE FUNCTION get_voice_positions(
    p_voice_id TEXT,
    p_days INTEGER DEFAULT 30
)
RETURNS TABLE (
    story_id UUID,
    story_headline TEXT,
    story_date DATE,
    story_slug TEXT,
    cluster_name TEXT,
    cluster_slug TEXT,
    quote TEXT,
    source_url TEXT,
    platform TEXT,
    topic_slugs TEXT[],
    confidence REAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.id AS story_id,
        s.headline AS story_headline,
        s.story_date,
        s.slug AS story_slug,
        c.name AS cluster_name,
        c.slug AS cluster_slug,
        cv.quote,
        cv.source_url,
        cv.platform,
        s.topic_slugs,
        cv.confidence
    FROM cluster_voices cv
    JOIN clusters c ON c.id = cv.cluster_id
    JOIN stories s ON s.id = cv.story_id
    WHERE cv.voice_id = p_voice_id
      AND s.story_date >= CURRENT_DATE - p_days
    ORDER BY s.story_date DESC, s.headline;
END;
$$ LANGUAGE plpgsql STABLE;


-- ============================================================================
-- 8. EDITORIAL TOOLS
-- ============================================================================

-- Content safety flags
CREATE TABLE content_flags (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type     TEXT NOT NULL,                   -- 'post', 'cluster_voice', 'story'
    target_id       UUID NOT NULL,
    flag_type       TEXT NOT NULL,                   -- 'safety', 'bias', 'misattribution', 'low_quality'
    reason          TEXT,
    is_resolved     BOOLEAN DEFAULT FALSE,
    flagged_by      TEXT DEFAULT 'pipeline',         -- 'pipeline' or editor email
    resolved_by     TEXT,
    flagged_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_flags_unresolved ON content_flags(target_type, is_resolved)
    WHERE is_resolved = FALSE;

-- Editorial overrides (cluster renames, voice removals, etc.)
CREATE TABLE editorial_overrides (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id        UUID REFERENCES stories(id),
    override_type   TEXT NOT NULL,                   -- 'cluster_rename', 'voice_remove', 'cluster_merge', 'story_unpublish'
    old_value       JSONB,
    new_value       JSONB,
    editor          TEXT NOT NULL,                   -- editor email
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Pipeline run log (for debugging and monitoring)
CREATE TABLE pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_date        DATE NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',          -- running, completed, failed
    posts_collected INTEGER DEFAULT 0,
    posts_categorized INTEGER DEFAULT 0,
    stories_created INTEGER DEFAULT 0,
    clusters_created INTEGER DEFAULT 0,
    errors          JSONB DEFAULT '[]',
    cost_usd        NUMERIC(6,4) DEFAULT 0
);


-- ============================================================================
-- 9. ROW-LEVEL SECURITY
-- ============================================================================
-- Pattern: Public anonymous read on all content tables.
-- Authenticated (editorial) write on flags + overrides.
-- Service role (pipeline) bypasses RLS entirely for writes.
--
-- Supabase anon key = public read.
-- Supabase service_role key = pipeline writes (server-side only, never exposed).
-- Supabase authenticated = editorial dashboard (jack@newsreel.co, etc.)
-- ============================================================================

ALTER TABLE voices ENABLE ROW LEVEL SECURITY;
ALTER TABLE topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE stories ENABLE ROW LEVEL SECURITY;
ALTER TABLE clusters ENABLE ROW LEVEL SECURITY;
ALTER TABLE cluster_voices ENABLE ROW LEVEL SECURITY;
ALTER TABLE content_flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE editorial_overrides ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;

-- PUBLIC READ: anyone with the anon key can read content
CREATE POLICY "Public read voices" ON voices FOR SELECT USING (true);
CREATE POLICY "Public read topics" ON topics FOR SELECT USING (true);
CREATE POLICY "Public read stories" ON stories FOR SELECT TO anon, authenticated
    USING (is_published = TRUE);
CREATE POLICY "Public read clusters" ON clusters FOR SELECT USING (true);
CREATE POLICY "Public read cluster_voices" ON cluster_voices FOR SELECT USING (true);

-- Posts: public read only for categorized posts (not raw uncategorized)
CREATE POLICY "Public read posts" ON posts FOR SELECT TO anon
    USING (topic_slug IS NOT NULL);
-- Authenticated users can read all posts (including uncategorized)
CREATE POLICY "Auth read all posts" ON posts FOR SELECT TO authenticated
    USING (true);

-- EDITORIAL WRITE: authenticated users can create flags and overrides
CREATE POLICY "Auth write flags" ON content_flags FOR INSERT TO authenticated
    WITH CHECK (true);
CREATE POLICY "Auth read flags" ON content_flags FOR SELECT TO authenticated
    USING (true);
CREATE POLICY "Auth write overrides" ON editorial_overrides FOR INSERT TO authenticated
    WITH CHECK (true);
CREATE POLICY "Auth read overrides" ON editorial_overrides FOR SELECT TO authenticated
    USING (true);

-- PIPELINE: pipeline_runs readable by authenticated only
CREATE POLICY "Auth read pipeline" ON pipeline_runs FOR SELECT TO authenticated
    USING (true);

-- NOTE: The service_role key bypasses RLS entirely. The pipeline uses
-- service_role for all INSERT/UPDATE operations on posts, stories,
-- clusters, cluster_voices, and pipeline_runs. This is the standard
-- Supabase pattern for server-side data ingestion.


-- ============================================================================
-- 10. HELPER VIEWS (non-materialized, for common query patterns)
-- ============================================================================

-- Story card view: everything needed to render a story card on the homepage
CREATE OR REPLACE VIEW v_story_cards AS
SELECT
    s.id,
    s.slug,
    s.headline,
    s.summary,
    s.story_type,
    s.heat_score,
    s.voice_count,
    s.cluster_count,
    s.topic_slugs,
    s.story_date,
    s.cover_url,
    (
        SELECT jsonb_agg(
            jsonb_build_object(
                'id', c.id,
                'name', c.name,
                'slug', c.slug,
                'voice_count', c.voice_count,
                'sort_order', c.sort_order,
                'best_quote_text', c.best_quote_text,
                'voices', (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'voice_id', cv.voice_id,
                            'name', v.name,
                            'photo_url', v.photo_url,
                            'quote', cv.quote,
                            'platform', cv.platform,
                            'source_url', cv.source_url
                        ) ORDER BY cv.quote_quality DESC
                    )
                    FROM cluster_voices cv
                    JOIN voices v ON v.id = cv.voice_id
                    WHERE cv.cluster_id = c.id
                )
            ) ORDER BY c.sort_order
        )
        FROM clusters c
        WHERE c.story_id = s.id
    ) AS clusters
FROM stories s
WHERE s.is_published = TRUE
ORDER BY s.story_date DESC, s.heat_score DESC;


-- Topic trending view: which topics have the most stories in last 7 days
CREATE OR REPLACE VIEW v_trending_topics AS
SELECT
    t.slug,
    t.display_name,
    COUNT(DISTINCT s.id) AS story_count,
    COUNT(DISTINCT cv.voice_id) AS voice_count,
    MAX(s.story_date) AS last_story_date
FROM topics t
JOIN stories s ON t.slug = ANY(s.topic_slugs)
JOIN cluster_voices cv ON cv.story_id = s.id
WHERE s.story_date >= CURRENT_DATE - 7
  AND s.is_published = TRUE
GROUP BY t.slug, t.display_name
ORDER BY story_count DESC, voice_count DESC;


-- ============================================================================
-- 11. TRIGGER: auto-update updated_at
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_voices_updated
    BEFORE UPDATE ON voices
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_stories_updated
    BEFORE UPDATE ON stories
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();


-- ============================================================================
-- 12. REFRESH ALIGNMENT SCORES (call after daily pipeline)
-- ============================================================================

CREATE OR REPLACE FUNCTION refresh_alignments()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_voice_alignments;
END;
$$ LANGUAGE plpgsql;
