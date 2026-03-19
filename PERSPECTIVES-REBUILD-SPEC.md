# Perspectives Rebuild Spec

**For:** Brijesh
**From:** Jack + Claude
**Date:** 2026-03-19
**Status:** Ready to build

---

## What exists today (prototype)

A working prototype at `newsreel.co/perspectives` that:
- Tracks 257 voices across 7 platforms (X, YouTube, Bluesky, TikTok, Instagram, Substack, podcasts)
- Collects posts daily, categorizes by topic with Claude Haiku (~$0.35/day)
- Groups voices into argument clusters per story (4-6 positions, not left/right)
- Serves via Python HTTP server on Render (`srv-d6pitsmuk2gs73fhkj70`)
- All data is JSON files on disk, no database

**Why it needs a rebuild:** It's inline HTML/JS files, no framework, no database, no auth, no tests, pipeline runs on Jack's Mac cron. Works for demos. Not shippable to libraries.

---

## Architecture

### Stack
- **Frontend:** Next.js (App Router) on Netlify, same domain as main site
- **Backend API:** Supabase (Postgres + Edge Functions + Realtime)
- **Pipeline:** Supabase cron (pg_cron) or Render cron service -- NOT Jack's Mac
- **AI:** Claude Haiku for categorization, Claude Sonnet for argument clustering
- **CDN:** wsrv.nl for voice photos (already used in newsletter)

### Database (Supabase Postgres)

```sql
-- Core tables
voices (
  id text PRIMARY KEY,           -- 'joe-rogan'
  name text NOT NULL,
  photo_url text,
  bio text,                      -- the lens/description
  category text,                 -- journalist, commentator, creator, politician, etc.
  approach text,                 -- investigates, explains, argues, reports, entertains
  tags text[],
  handles jsonb,                 -- {x: 'joerogan', youtube: 'joerogan', ...}
  feeds jsonb,                   -- {youtube: 'https://...', x: 'https://rss.app/...'}
  followers_display text,        -- '39.9M'
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

posts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  voice_id text REFERENCES voices(id),
  platform text NOT NULL,        -- x, youtube, bluesky, tiktok, instagram, substack, podcast
  text text NOT NULL,
  quote text,                    -- extracted best quote (may differ from full text for transcripts)
  source_url text,
  topic text,                    -- from taxonomy: 'iran-conflict', 'immigration', etc.
  relevance text,                -- high, medium, low
  stance text,                   -- strong, lean, neutral
  collected_date date NOT NULL,
  published_at timestamptz,
  created_at timestamptz DEFAULT now()
);
CREATE INDEX idx_posts_voice_date ON posts(voice_id, collected_date);
CREATE INDEX idx_posts_topic_date ON posts(topic, collected_date);

stories (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  headline text NOT NULL,
  summary text,
  slug text UNIQUE NOT NULL,
  story_type text,               -- split, spectrum, consensus, reaction
  source text,                   -- 'voices' (auto-detected) or 'editorial' (CMS)
  heat_score integer DEFAULT 0,
  voice_count integer DEFAULT 0,
  cluster_count integer DEFAULT 0,
  cover_url text,
  topic_slugs text[],
  story_date date NOT NULL,
  created_at timestamptz DEFAULT now()
);
CREATE INDEX idx_stories_date ON stories(story_date DESC);

clusters (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  story_id uuid REFERENCES stories(id) ON DELETE CASCADE,
  name text NOT NULL,             -- 'Anti-War Coalition', 'Military Supporters'
  voice_count integer DEFAULT 0,
  sort_order integer DEFAULT 0,
  best_quote_voice_id text,
  best_quote_text text,
  best_quote_platform text
);

cluster_voices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cluster_id uuid REFERENCES clusters(id) ON DELETE CASCADE,
  voice_id text REFERENCES voices(id),
  quote text,
  source_url text,
  platform text,
  quote_quality integer DEFAULT 5,  -- 1-10 fit score from validation
  UNIQUE(cluster_id, voice_id)
);
CREATE INDEX idx_cluster_voices_voice ON cluster_voices(voice_id);

-- For the "aligned voices" feature on profiles
-- Materialized or computed: which voices end up in the same cluster most often
voice_alignments (
  voice_a text REFERENCES voices(id),
  voice_b text REFERENCES voices(id),
  shared_clusters integer DEFAULT 0,
  total_stories integer DEFAULT 0,
  alignment_score float DEFAULT 0,  -- shared_clusters / total_stories
  updated_at timestamptz DEFAULT now(),
  PRIMARY KEY(voice_a, voice_b)
);

-- Taxonomy
topics (
  slug text PRIMARY KEY,
  display_name text NOT NULL,
  description text,
  aliases text[]
);

-- Content safety
content_flags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id uuid REFERENCES posts(id),
  flag_type text,                -- 'safety', 'bias', 'accuracy'
  reason text,
  flagged_at timestamptz DEFAULT now()
);

-- Editorial overrides (from review dashboard)
editorial_overrides (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  story_id uuid REFERENCES stories(id),
  override_type text,            -- 'cluster_rename', 'voice_remove', 'cluster_merge'
  old_value text,
  new_value text,
  editor text,
  created_at timestamptz DEFAULT now()
);
```

### Row Level Security
- Public read on: voices, stories, clusters, cluster_voices, topics
- Authenticated write on: editorial_overrides, content_flags
- Service role only: posts (pipeline writes)

---

## Pipeline (runs daily, NOT on Jack's Mac)

### Option A: Supabase Edge Function + pg_cron
```
pg_cron triggers at 6am ET daily:
  1. Edge Function: collect_posts() -- pulls from all 7 platforms
  2. Edge Function: categorize_posts() -- Claude Haiku categorizes by topic
  3. Edge Function: build_stories() -- detects top stories from topic convergence
  4. Edge Function: cluster_voices() -- Claude Sonnet assigns argument clusters
  5. Edge Function: compute_alignments() -- update voice_alignments table
```

### Option B: Render cron service (simpler, keep Python)
Keep the existing Python pipeline but deploy it as a Render cron job instead of a local Mac cron. Writes directly to Supabase via postgrest.

**Recommendation:** Option B for v1 (faster to ship, Python pipeline already works). Migrate to Edge Functions later.

### Pipeline cost
- Collection: free (RSS, public APIs)
- Categorization: ~$0.35/day on Claude Haiku (257 voices)
- Clustering: ~$0.50/day on Claude Sonnet (~10 stories x 1 call each)
- Total: ~$0.85/day / ~$26/month

---

## Frontend (Next.js)

### Pages

```
/perspectives                  -- Homepage: today's stories feed
/perspectives/story/[slug]     -- Story detail: clusters, voices, quotes
/perspectives/voice/[id]       -- Voice profile: positions, aligned voices
/perspectives/voices           -- Voice directory: grid, search, filter
/perspectives/search           -- Search any topic
/perspectives/methodology      -- How it works
```

### Design system (match the app)

The app screenshot shows the right design. Every story card should look like:

```
+------------------------------------------+
| Multiple Terror Attacks Strike US         |
| 25 voices  4 positions                   |
|                                           |
|  [face][face][face]  vs  [face][face]    |
|  National Security       Community        |
|  Focus (14)              Response (5)     |
|                                           |
|  [====blue====][==red==][=yellow=]       |
|                    See 4 positions ->      |
+------------------------------------------+
```

Key design rules:
- Every story card is the same size/layout (no tier-top, tier-medium, tier-compact)
- Two dominant clusters shown face-to-face with "vs" between them
- Color proportion bar shows all clusters at a glance
- "See N positions" links to full story page
- Voice bios visible on hover/tap (already implemented)
- Dark theme, DM Sans font, #FF6343 accent (same as current)
- Mobile-first

### Voice profile page

```
+------------------------------------------+
| [photo]                                   |
| Joe Rogan                                 |
| Comedian & long-form interviewer...       |
| [libertarian] [anti-establishment] [free] |
| [X] [YouTube] [Bluesky]                  |
|                                           |
| POSITIONS (5 topics today)                |
| +--------------------------------------+ |
| | iran-conflict [Anti-War Coalition]   | |
| | "quote text here..."                 | |
| | Others in cluster: [face][face]      | |
| +--------------------------------------+ |
|                                           |
| ALIGNED WITH (based on 8 stories)         |
| [face] Tucker Carlson  6/8               |
| [face] Glenn Greenwald 5/8              |
+------------------------------------------+
```

---

## API endpoints (Supabase Edge Functions or postgrest)

```
GET  /api/stories?date=2026-03-19      -- today's stories with clusters
GET  /api/stories/[slug]               -- single story with full cluster data
GET  /api/voices                       -- all voices (for directory)
GET  /api/voices/[id]                  -- single voice with positions + alignments
GET  /api/voices/[id]/positions        -- voice's topic positions from recent posts
GET  /api/search?q=immigration         -- topic search with concept expansion
GET  /api/wire?date=2026-03-19         -- raw post feed
GET  /api/topics                       -- trending topics with counts
POST /api/editorial/override           -- editorial review submission (auth required)
```

All public GETs served via Supabase postgrest with RLS. No custom server needed.

---

## Migration path

### Phase 1: Database + pipeline (Brijesh, 1-2 weeks)
1. Create Supabase tables from schema above
2. Migrate `voices.json` into `voices` table
3. Migrate `taxonomy.json` into `topics` table
4. Deploy existing Python pipeline as Render cron writing to Supabase
5. Verify daily pipeline runs reliably for 3 days

### Phase 2: API + frontend (Brijesh, 2-3 weeks)
1. Build Next.js app with pages listed above
2. Wire to Supabase postgrest for all data
3. Implement the app-style card design (vs layout, proportion bar)
4. Voice profiles with positions + alignments
5. Search with concept expansion (move CONCEPT_MAP to Supabase config)
6. Deploy on Netlify at `newsreel.co/perspectives`

### Phase 3: Polish (1 week)
1. Editorial review dashboard (auth'd)
2. Content safety filtering at query time
3. Tag tooltips with glossary definitions
4. Mobile optimization
5. OpenGraph meta tags for story sharing

### What stays the same
- 257 voices and their bios (already clean)
- Argument cluster methodology (Claude prompts)
- Topic taxonomy
- Collection sources (RSS, APIs)
- Cost structure (~$26/month AI)

### What changes
- JSON files on disk -> Postgres
- Python HTTP server -> Next.js + Supabase
- Mac cron -> cloud cron
- Inline HTML/JS -> React components
- 5 separate HTML files -> shared design system
- No auth -> Supabase auth for editorial tools

---

## What NOT to build

- User accounts / login (not needed for v1, libraries use it without auth)
- Comments / discussion (Nicole asked, but this is a viewing tool)
- Fact-checking layer (we show what people say, not whether it's true)
- Real-time updates (daily refresh is fine, we're not a wire service)
- Mobile app (web is the product for libraries, the Newsreel app is separate)

---

## Success criteria

- Pipeline runs daily without Jack touching anything
- Fresh stories every morning by 7am ET
- Voice profiles always have recent positions (not empty)
- Page load under 2 seconds
- Works on school/library network (no blocked CDNs)
- Passes Nicole Manning's 10/10 test (labels, safety, representation)
- Eliza can immediately see who every voice is without clicking through
