# Exa Connect provider plan for Newsreel Perspectives

**Date:** 2026-09-14
**Status:** DRAFT, for Jack + Brijesh review; nothing built yet.
**Scope:** Read-only investigation of this repo (git remote `jbrew21/newsreel-perspectives`), Exa's public pages, the Exa Connect ToS PDF David Nogueira sent Sep 14, and the email thread `1a0872484494d314`. No file was modified during planning; nothing touched Supabase, Render, or GitHub.
**Standing decisions (Jack, Sep 14):** sell the derived layer only (citation index, never full `text`); Instagram collection stays in the pipeline and on the site; keep X collection logged out; zero retention of Exa customer queries; a takedown path for any voice.

---

## 0. What Exa Connect actually needs (confirmed vs. not confirmed)

**Confirmed (fetched Sep 14):**

- Exa Connect lets agents "access partner data" inside Exa Agent runs. Agents call providers through Exa's Agent API (`POST https://api.exa.ai/agent/runs`) by naming the provider in `dataSources: [{"provider": "<id>"}]` plus an `outputSchema`. Exa states it "handles the plumbing" including "provider authentication, tool selection, retries, and result ranking" (exa.ai/docs/reference/agent-api/connect/overview).
- Pricing: "Provider calls are billed per call, currently $0.005 to $0.03 depending on the provider, additive to standard Agent pricing" (exa.ai/connect). Listed rates: Similarweb $0.03/call, Fiber.ai $0.02/credit, Affiliate.com $0.015, Particle $0.015, Financial Datasets $0.01, Jinko $0.005, Polymarket free, Baselayer $0.10–$4.00/order.
- Providers apply via a Google Form; David's email asks for "Link to your API/MCP documentation (endpoints, auth, rate limits)", so Exa accepts either a documented REST API or an MCP server and wraps it as agent tools.
- Exa Connect Terms of Service (Last Revised Aug 28, 2026; `exa.ai/assets/Exa_Connect_Terms_of_Service.pdf`), read in full. Relevant clauses: 1.1 "Customer Data" = "queries, prompts, and other inputs"; 2.2 Provider controls listing and display, Exa may "modify, reformat, summarize, excerpt"; 2.4 Provider must supply its own "Provider Data Terms" for customers; 2.8 listing changes need one week's notice and are at Exa's discretion; 3.2–3.4 Stripe billing, payout "calculation methodology" and "payment terms" are "mutually agreed by Company and Provider in writing"; 4.2 license to Exa with "transiently cache" and an explicit no-training clause; 4.5 accuracy warranty; 4.6 rights warranty; 4.7 Customer Data restrictions incl. "in no event shall Provider retain, store, log, or otherwise persist any Customer Data beyond the time reasonably necessary to process the specific request", referencing a "Zero Data Retention Addendum to the Master Subscription Agreement"; 4.9 Provider Data "shall not include any Personal Data" with carve-out warranties incl. not "obtained through deceptive practices, unauthorized access, or in violation of any applicable laws or terms of service"; 7.2 liability caps (2x fees; indemnity 3x fees); 7.3 provider indemnity; 8.2 sixty-day termination for convenience with a duty to keep serving during notice; 9.1 California law, San Francisco courts; contact `data-partners@exa.ai`.
- Exa's own ZDR definition: a provider "must never store user query data, neither in the main service nor any subprocessors" (exa.ai/blog/zdr-search-engine).

**Not confirmed (no public provider-facing spec exists):** whether Exa prefers REST or MCP; the request/response shape Exa expects; how Exa authenticates to a provider; timeouts or latency requirements; Exa's take rate and payout schedule (ToS says "mutually agreed"); the text of the ZDR Addendum and MSA (not public); how long Exa "transiently" caches provider responses. All of these go in the question list for David (section 6).

---

## 1. Current state (10 lines)

1. Data flow: `scripts/collect.py` pulls posts per voice (X via logged-out guest token `scripts/x_guest.py`, Bluesky public API, YouTube RSS + transcripts, Substack/blog/podcast RSS, TikTok via yt-dlp, Instagram via a burner-account Chrome cookie jar at `collect.py:663-676`), categorizes with Claude Haiku 4.5 (`collect.py:1108-1199`), keeps only `relevance in {high,medium}` and `stance in {strong,lean}` (`collect.py:1193-1195`).
2. Outputs per run: `data/posts/<voice>/<YYYY-MM-DD>.json` (raw `text` up to 500 chars + derived `topic/topics/relevance/stance/summary/quote`), `data/posts/topic-index-<date>.json`, `stories-<date>.json`; `scripts/build_stances.py` merges every kept post into the durable per-voice store `data/stances/<voice>.json` (no age-out, `build_stances.py:34-38`; quote capped at 280 chars, summary at 120, line 94-95).
3. Scheduler: GitHub Actions `.github/workflows/daily-pipeline.yml`, cron 06:17/08:17/17:17/19:17 UTC, steps collect → enrich → stories → build_stances → verify index → collection health → Supabase sync (`continue-on-error`) → prune (aggregates 30d, per-voice files 45d) → commit/push → Render deploy of `srv-d6pitsmuk2gs73fhkj70`.
4. Supabase: project host `jkvxoshrdnfahcfojryb.supabase.co` (from `.env` key `SUPABASE_URL`; no keys read). Schema in `SUPABASE-SCHEMA.sql`: `voices, topics, posts (monthly partitions), stories, clusters, cluster_voices, content_flags, editorial_overrides, pipeline_runs`. `posts.text` holds up to 5,000 chars (`migrate_to_supabase.py:237`) and is readable by the anon role wherever `topic_slug IS NOT NULL` (`SUPABASE-SCHEMA.sql:444-445`).
5. Site: Render web service `newsreel-perspectives` (`render.yaml`), `python serve.py`, free tier (workflow line 122 comment), host `newsreel-perspectives.onrender.com` (`scripts/voices_lib.py:34`). Endpoints `/api/lookup`, `/api/ask`, `/api/stories`, `/api/topics`, `/api/wire`, `/api/agenda`, `/api/voice-*`, plus static `data/*.json` served bare (`serve.py:1061-1063`).
6. Search on the site sends the query to Claude (`scripts/lookup.py:232-295`), logs it (`serve.py:843, 862`), caches it by md5 (`serve.py:639-641, 677`) and writes `data/results/<query-slug>.json` (`lookup.py:970-977`). None of this may be reused for Exa.
7. Voices: 334 in `data/voices.json` (commentator 77, politician 63, journalist 53, creator 45, think-tank 29, economist 20, activist 13, medical 11, legal 10, satirist 8, entrepreneur 5). Handles: X 274, Instagram 139, Bluesky 128, YouTube 91 (+99 feeds), TikTok 83, Substack 58 feeds, podcast 9, blog 7.
8. Tests: 12 files under `tests/` (unittest, 205 tests) but no CI test step in the workflow.
9. Least new infrastructure for an API: a second Render web service defined in the same `render.yaml`, deployed by the same daily deploy hook, reading a prebuilt in-memory index. Zero Supabase changes, zero new DNS, one secret.
10. Instagram collection cannot run in CI (needs Jack's Mac Chrome profile; `data/collection-baseline.json:13`); the corpus holds 8 Instagram documents.

---

## 2. Corpus sizing (counted from local `data/` on 2026-09-14)

**What is a "document":** one `(voiceId, sourceUrl)` pair with a taxonomy topic, a `strong|lean` stance, a summary, and a canonical link. Raw scrape volume is much larger and must not be quoted.

| Measure | Value | Source / label |
|---|---|---|
| Sellable documents today (stance store ∪ last-45-day day files, filtered) | **44,259** | counted; 32,707 in `data/stances/` + 43,825 passing filters in `data/posts/`, union deduped |
| Distinct voices with ≥1 document | **326** of 334 | counted |
| By platform | X 26,696 · Bluesky 8,112 · YouTube 6,926 · TikTok 1,427 · Substack 844 · blog 210 · podcast 36 · Instagram 8 | counted |
| Topics | 40 taxonomy slugs (41 incl. `other`, never served) | `data/taxonomy.json` |
| Published in last 30 / 90 days | 13,036 / 28,297 | counted by post timestamp |
| Published before 2026 (feed backfill) | 15,420; earliest 2012-10-31 | counted |
| Median documents per voice | 96.5 (top: meidastouch 394, benny-johnson 354, robert-reich 316) | `data/stances/` |
| Growth, new documents/day | **~450 by publish date** (avg Aug 31–Sep 13: 446) · ~550 by first-seen (Sep 3–13: 546) | estimate; X was dead Aug 19–30, restored Aug 30 |
| Growth/month | **~13,000–16,000** | estimate from the two daily figures |
| Summaries present | 43,704 of 43,825 day-file docs; 32,600 of 32,707 stances | counted |
| Quote length (stance store) | median 29 words, p90 47; 17,859 of 32,707 (55%) exceed 25 words | counted; truncation is mandatory |
| Raw scrape objects per run | ~18,100 (X ~15,700; Bluesky ~1,300; YouTube ~620) | `data/posts` day files, Sep 13 |
| Local window | 14,697 day files, 370,277 post records, 46,167 distinct docs, dates 2026-07-30 to 2026-09-13 | counted |
| Latest topic index | 40 topics, 20,782 entries, 17,680 distinct URLs, 312 voices | `topic-index-2026-09-13.json` |

**Supabase caveat:** VERTICALS.md reports "209K posts archived, +7K/day". That is a row count, not a document count: `migrate_to_supabase.py:230` builds the row id from `(voice_id, platform, source_url, collected_date)`, so a post re-scraped on N days becomes N rows. In the local window that ratio is 8.0x (368,388 rows vs 46,159 distinct posts). Brijesh should run `SELECT count(*), count(DISTINCT (voice_id, source_url)) FROM posts` before any Supabase number goes to David. Not verified this session.

**David-ready figures:** ~44,000 indexed positions from 326 named voices across 7 platforms and 40 topics; ~13,000 published in the last 30 days; growing roughly 450–550 documents per day (~15,000/month); coverage dense from July 2026, sparse back to 2012.

---

## 3. Endpoint design

### 3.1 Hosting: separate FastAPI service on Render, in this repo

Recommendation: `api/exa_api.py` (FastAPI + uvicorn, own `api/requirements.txt`) as a second `web` entry in `render.yaml`, Starter plan, reading a prebuilt `data/exa-index.json` into memory at boot. The index is built in Render's `buildCommand` (`python scripts/build_exa_index.py`) from the committed `data/stances/` and `data/posts/`, so the existing post-pipeline deploy hook refreshes it daily with no new scheduler.

Why not the alternatives:
- **Routes inside `serve.py`:** zero new infra, but `serve.py:718-720` logs every request line, `serve.py:843/862` log queries, the service is free tier (cold starts of tens of seconds would time out Exa calls), and the ZDR audit surface becomes all 1,153 lines. Fallback only if Brijesh cannot create a service.
- **Supabase Edge Function:** every change needs Brijesh; PostgREST/Edge logs record request URLs; posts.text already sits behind the anon key; no benefit at 44K rows.
- **Static prebuilt index alone:** Exa needs a query endpoint. The static index is the data layer of the recommended design, not a substitute.

Cost: Render Starter ~$7/month. Maintainer: a Claude session in this repo for code; Brijesh for the Render service, plan, secret, and optional domain.

### 3.2 Endpoints (`/v1`, versioned; no Exa-specific naming so TollBit/Cloudflare can reuse it)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/search` | key | Positions matching a JSON body. Query text travels in the body, never the URL. |
| GET | `/v1/positions/{id}` | key | One document by id. |
| GET | `/v1/voices` | key | Directory; filters `category`, `limit`, `cursor`. |
| GET | `/v1/voices/{voice_id}` | key | Profile + topic counts. |
| GET | `/v1/voices/{voice_id}/positions` | key | Grouped by topic; `topic`, `since`, `until`, `limit`, `cursor`. |
| GET | `/v1/topics` | key | Taxonomy with display names, descriptions, document counts. |
| GET | `/v1/health` | none | `index_date`, `document_count`, `voice_count`, contact. |
| GET | `/openapi.json`, `/docs` | none | Auto-generated by FastAPI; this is the "API documentation" link for David. |

**Search request body**

```json
{
  "q": "Iran strikes",
  "topics": ["iran-conflict"],
  "voice_ids": ["adam-schiff"],
  "categories": ["politician"],
  "platforms": ["x", "bluesky"],
  "stance": "strong",
  "since": "2026-08-15", "until": "2026-09-14",
  "limit": 20,
  "cursor": "eyJvIjoyMCwidiI6IjIwMjYtMDktMTMifQ"
}
```

All fields optional. `topics` bypasses `q` mapping; `platforms` must be within `SERVED_PLATFORMS`; `stance` is `strong | lean`; `limit` default 20, max 50.

**Search response**

```json
{
  "index_date": "2026-09-13",
  "matched_topics": ["iran-conflict", "military-defense"],
  "total": 412,
  "results": [ { "...Position..." : "" } ],
  "next_cursor": "eyJvIjo0MCwidiI6IjIwMjYtMDktMTMifQ"
}
```

**Position (the only document shape ever emitted)**

```json
{
  "id": "3f1c9a7b2e4d6c01",
  "voice": { "id": "adam-schiff", "name": "Adam Schiff", "category": "politician",
             "approach": "legislates", "lens": "one-line editorial descriptor" },
  "topic": { "slug": "healthcare", "display": "Healthcare" },
  "topics": ["healthcare"],
  "stance": "strong",
  "summary": "Advocates Medicare for All as bold Democratic agenda priority",
  "quote": "If Democrats take the majority, it's not enough for us to go back to the status quo before Trump came into office. We need…",
  "date": "2026-09-12",
  "platform": "x",
  "source_url": "https://x.com/SenAdamSchiff/status/2098851980026184106",
  "attribution": "Adam Schiff, X, 2026-09-12, via Newsreel Perspectives",
  "profile_url": "https://newsreel-perspectives.onrender.com/voice/adam-schiff"
}
```

Allowed keys are exactly the ones above. **Excluded, always:** `text`, `timestamp` (date only), `relevance`, `confidence`, `type`, transcripts, `handles`, `feeds`, `photo`, `photoSource`, `followers`, `followersDisplay`, `tags`, `topicSummary`, anything from `stories`/`clusters`.

- `id` = `sha1("{voiceId}|{sourceUrl}")[:16]`.
- `quote` = first 25 words of the stored `quote` (real post text, never generated; `collect.py:1032-1038`), then "…" if truncated.
- `summary` = the Haiku 4-8 word label (`collect.py:1124`), ≤120 chars.

**Search semantics (no LLM, no network in the request path):**
1. `topics` given → use as-is (validated against taxonomy).
2. Else map `q` locally: `lookup._keyword_match` (CONCEPT_MAP + slug words, `scripts/lookup.py:297-370`), plus taxonomy `aliases` the way `collect.enforce_taxonomy` does (`collect.py:904-919`), then `lookup.drop_unnamed_broad_topics` (`lookup.py:112-116`).
3. Score = 3 × primary-topic match + 1 × secondary-topic match + token overlap of `q` against `summary + quote` (inverted index built at load) + recency decay (30-day half-life); ties newest first. Filters (`voice_ids`, `categories`, `platforms`, `stance`, dates) apply before ranking.
4. Pagination: opaque base64 cursor `{offset, index_date}`; a cursor from a stale index returns `400 invalid_cursor`.

**Errors:** `{"error": {"code": "...", "message": "..."}}` with `invalid_request` 400, `unauthorized` 401, `not_found` 404, `rate_limited` 429 (+`Retry-After`), `index_unavailable` 503.

**Auth:** `Authorization: Bearer <key>` (also accept `x-api-key`). Keys from env `API_KEYS="exa-2026-09:<32 random chars>,exa-next:<…>"`; two keys can be live at once, so rotation is: add the new key, give it to David, remove the old one on the next deploy. Keys are compared with `hmac.compare_digest`. One key per partner (Exa, later TollBit).

**Rate limits (per key, in memory):** 300 requests/min, 20/sec burst, 100,000/day soft cap; 429 with `Retry-After`. Counters hold only `(key_name, minute, count)` and `(key_name, day, count)`.

**Timeouts / latency / uptime:** in-memory index, expect p95 < 300 ms at 44K documents; server hard limit 10 s; suggest Exa use a 5 s client timeout. Target 99.5% monthly availability on a single Starter instance, no SLA in v1; `/v1/health` for probes; `index_date` tells Exa how fresh the data is.

**MCP:** not built in v1. If David says Connect needs MCP, add `api/mcp_server.py` (streamable HTTP) exposing three tools (`search_positions`, `get_voice`, `list_topics`) that call the same serializer and index; ~4 hours (Phase 6).

---

## 4. Zero-retention design

**What the service logs:** one startup line (index date, document count), one line per index reload, and an hourly aggregate line `{"key": "exa-2026-09", "day": "2026-09-14", "calls": 1234, "errors": 3}`. Nothing else.

**What it never does:** no uvicorn access log (`--no-access-log`); no logging of request bodies, `q`, topic lists, voice ids, or client IPs; no response cache keyed on the query (the only in-memory state is the index, the suppression list, and the counters above); no `data/results/` writes; no Claude or any third-party call inside a request; no database in the request path; no Sentry/analytics SDK. The search endpoint is `POST` with a JSON body specifically so query text never appears in a URL that a proxy or platform log could capture.

**Hosting-specific:** Render keeps HTTP request logs (method, status, host, path) for Professional workspaces and above (render.com/docs/logging). Paths of our endpoints carry no query content (`/v1/search`, `/v1/voices/<id>`), so even if those logs are on they hold no Customer Data; Brijesh should confirm the workspace tier and, if a custom domain is fronted by Cloudflare, that Cloudflare logging is off for the API host. If the index ever moves into Supabase, use `POST` RPC calls only; PostgREST logs URLs.

**Proof:** (1) `api/exa_api.py` has no `logging`/`print` call that receives request data, enforced by `tests/test_exa_api.py::test_query_never_logged` (captures root logger + stdout at DEBUG, calls every endpoint with a sentinel query string, asserts the sentinel appears in no log line and no file under `data/`); (2) `docs/ZERO-RETENTION.md` reproducing the statement below plus the exact uvicorn flags; (3) the open-source repo.

**Statement for David (plain facts, not email prose):**
- The Newsreel Perspectives API does not log, store, cache, or persist queries or any content sent through the API.
- Requests are processed against an in-memory index; no request data is written to disk, to a database, or sent to any subprocessor or model provider.
- The only retained metrics are per-API-key daily and per-minute call counts, which contain no query content.
- Access logging is disabled at the application level; the search endpoint accepts query text in the request body so it never appears in a URL.
- This matches ToS 4.7 and Exa's ZDR definition ("never store user query data, neither in the main service nor any subprocessors").

---

## 5. Serve-time filters (all in one module: `scripts/exa_index.py`)

```python
# scripts/exa_index.py (new)
SERVED_PLATFORMS = frozenset({"x", "bluesky", "youtube", "tiktok", "substack",
                              "blog", "podcast", "instagram"})   # remove "instagram" here to drop it
MAX_QUOTE_WORDS = 25
ALLOWED_POSITION_KEYS = frozenset({"id", "voice", "topic", "topics", "stance", "summary",
                                   "quote", "date", "platform", "source_url", "attribution", "profile_url"})
ALLOWED_VOICE_KEYS = frozenset({"id", "name", "category", "approach", "lens"})
FORBIDDEN_KEYS = frozenset({"text", "timestamp", "relevance", "confidence", "type", "handles",
                            "feeds", "photo", "photoSource", "followers", "followersDisplay", "tags"})
SUPPRESSIONS_PATH = ROOT / "data" / "exa-suppressions.json"
```

- **Platform allowlist:** `SERVED_PLATFORMS` is the single constant; Instagram is in by default per Jack (Sep 14). Note for counsel: today that is 8 documents, because Instagram collection only runs on Jack's Mac (`data/collection-baseline.json:13`). Switching it out is deleting one token.
- **Suppression list (takedown):** `data/exa-suppressions.json` = `{"updated": "...", "voices": ["voice-id"], "urls": ["https://…"], "reason": {...}}`. Checked at build time and again at serve time (mtime-cached reload every 60 s, same pattern as `serve.py:396-419 load_voice_meta`), so a file edit + deploy or a hot edit on the instance both take effect. Site and stance store are untouched; this removes a voice only from the sold index.
- **Takedown mechanism:** contact listed in `/v1/health`, in the OpenAPI description, in the Provider Data Terms (ToS 2.4) and on the methodology page: `perspectives@newsreel.co` (or `jack@newsreel.co`; Jack decides). Any tracked voice, their representative, or a platform rightsholder can request removal; acknowledge within 2 business days, remove within 5; keep a dated line in the suppression file. No proof-of-identity theatre for a removal request.
- **Quote truncation:** `truncate_words(quote, 25)` in the serializer; test asserts every emitted `quote` ≤ 25 words + optional ellipsis.
- **Never-serve-`text` rule:** exactly one function, `serialize_position(post, voice)`, builds output dicts from an explicit allowlist; it never copies the input dict. `assert_clean(obj)` walks any response recursively and raises on any key in `FORBIDDEN_KEYS` or any key outside the allowlists; the API calls it on every response in debug/test mode and the index builder calls it on every document.
- **Other build-time filters:** taxonomy topic present and ≠ `other`/`uncategorized`; `stance ∈ {strong, lean}`; non-empty `summary`; `build_stances.looks_like_promo` (`build_stances.py:67-78`); content safety terms from `serve.py:66-67`/`lookup.py:45-49`; dedupe by `(voiceId, sourceUrl)`, newest categorization wins.
- **X provenance guard:** the builder reads only `data/stances/` and `data/posts/`, which are written solely by `collect.py`; X rows originate from `scripts/x_guest.py` (guest token, `x_guest.py:42-43, 227-263`). `tests/test_exa_index.py::test_no_official_x_api_in_repo` greps `scripts/` and `api/` for `api.x.com/2`, `api.twitter.com/2`, `TWITTER_BEARER`, `X_API_KEY` and fails on any hit. The `.env` here holds only `ANTHROPIC_API_KEY, RENDER_API_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL` (key names only were read).
- **Build-failing test:** `tests/test_exa_api.py::test_no_raw_text_anywhere` spins the FastAPI `TestClient` against a fixture index that deliberately contains `text`, `handles`, `feeds`, `photo`, `followers`, a 60-word quote and a suppressed voice; calls every endpoint with every filter; asserts no forbidden key, no quote > 25 words, no suppressed voice, no non-allowlisted platform. Wire `python -m unittest discover -s tests` into a new `.github/workflows/tests.yml` on push/PR (there is no CI test step today) and into the Render `buildCommand` so a failing test blocks the deploy.

---

## 6. Pricing sheet draft (`docs/EXA-PRICING.md`)

**Newsreel Perspectives API — pricing for Exa Connect**

- Model: usage-based, billed per provider call through Exa.
- Price: **$0.03 per call**, flat, all endpoints. (Alternative if Exa supports per-tool pricing: $0.03 for `/v1/search` and `/v1/voices/{id}/positions`; $0.01 for `/v1/voices`, `/v1/topics`, `/v1/positions/{id}`.)
- One call returns up to 50 attributed positions (voice, stance label, one-line summary, date, platform, canonical link).
- No minimums, no setup fee, no tiers. Errors (4xx/5xx) and empty results should not be billed; confirm Exa's mechanics.
- Data: ~44,000 positions today, 326 named voices, 40 topics, 7 platforms, growing ~15,000/month; refreshed daily.
- Terms: citation index. Full post text is not served; agents are sent to the source link. Attribution required ("via Newsreel Perspectives"). No re-hosting of the index. Removal requests: perspectives@newsreel.co.

**Rationale for top of range:** each call returns dozens of structured, deduplicated, dated positions across 300+ named people, produced by a daily editorial roster plus per-post model categorization; the closest structured-signal comparable on the list (Similarweb) is $0.03; demand at these volumes is not price-elastic (Jack, Sep 14: "low pricing does not create demand here"); it is easier to cut than to raise; ToS 2.8 requires one week's notice for listing changes anyway.

**Open questions for David:**
1. Exa's take rate on the $0.03 and the standard payout methodology (ToS 3.3 says "mutually agreed in writing"; send the standard order form).
2. Payout threshold, frequency and timing (Stripe; monthly? net-30?), and reporting we get (calls per day per customer domain).
3. Can the price change later, with what notice; per-provider price only, or per-tool?
4. Billed per call regardless of result count or errors?
5. REST + OpenAPI acceptable, or is an MCP server required? What request/response shape does Exa's wrapper expect, and what timeout does it use?
6. How does Exa authenticate to us (static bearer key we issue)? Fixed egress IPs to allowlist?
7. Copies of the Master Subscription Agreement and the Zero Data Retention Addendum referenced in 4.7.
8. Does Exa cache provider responses, and for how long (4.2 "transiently cache")?
9. Where do our Provider Data Terms (2.4) get shown to customers: link in the listing, or hosted by Exa?
10. Does 4.10 (do not block Exa's crawlers) apply to newsreel.co as a whole or only to the provider docs?

---

## 7. Answers to David's five questions (facts only; the voice skill writes the email)

1. **Usage-based pricing:** yes. Per-call, flat rate, no minimum.
2. **Pricing doc:** $0.03 per call across all endpoints (or $0.03 search / $0.01 lookups if per-tool pricing exists); one-page sheet at `docs/EXA-PRICING.md`, to be published at a newsreel.co URL once Brijesh deploys.
3. **API docs:** REST with OpenAPI at `https://<api-host>/docs` and `/openapi.json`. Bearer API key issued per partner, rotatable. Rate limit 300 requests/min and 100,000/day per key; 429 with Retry-After. Endpoints: `POST /v1/search`, `GET /v1/voices`, `GET /v1/voices/{id}`, `GET /v1/voices/{id}/positions`, `GET /v1/topics`, `GET /v1/positions/{id}`, `GET /v1/health`. Typical latency under 300 ms. MCP wrapper available on request.
4. **Corpus:** ~44,000 indexed positions (documents) from 326 named public voices across X, Bluesky, YouTube, TikTok, Substack, blogs and podcasts, organized under 40 news topics. ~13,000 published in the last 30 days. Growing ~450–550 documents/day (~15,000/month); index refreshed daily. Each document: voice, topic, stance label, one-line summary, date, platform, a ≤25-word attributed quote and the canonical source link. Full text is not part of the product.
5. **Zero data retention:** confirmed as defined in ToS 4.7. No query logging, no query caching, no request bodies persisted, no third-party or model call in the request path, no database in the request path. Only per-key aggregate call counts are kept for billing. Written statement and configuration in `docs/ZERO-RETENTION.md`.

---

## 8. Work plan (ordered for the fastest demo)

| Phase | Owner | Hours | Deliverable |
|---|---|---|---|
| 0. Decisions | Jack | 0.5 | Price ($0.03 vs $0.02), takedown contact email, Instagram default (stays in), Render plan approval, whether to reframe the Exa pitch from "contributor corpus" to Perspectives (see risks). |
| 1. Index + serializer | Claude session | 5 | `scripts/exa_index.py` (constants, `serialize_position`, `serialize_voice`, `assert_clean`, `truncate_words`, suppression loader, local topic mapper reusing `lookup._keyword_match`), `scripts/build_exa_index.py` (writes `data/exa-index.json` + `data/exa-index.meta.json`; add both to `.gitignore`), `data/exa-suppressions.json` (empty), `tests/test_exa_index.py` (allowlist, forbidden keys, 25-word truncation, platform allowlist, suppression, promo/safety filters, topic mapping, X-API grep guard). Run `python -m unittest discover -s tests`. |
| 2. API | Claude session | 5 | `api/exa_api.py` (FastAPI app, bearer auth with rotation, per-key limiter, cursor pagination, error shapes, `/v1/health`, no access log), `api/requirements.txt` (fastapi, uvicorn, httpx for tests), `tests/test_exa_api.py` (`test_no_raw_text_anywhere`, `test_query_never_logged`, auth, 429, cursor). Local demo: `API_KEYS=demo:x uvicorn api.exa_api:app` then curl `/v1/search`. **Demo-able at the end of Phase 2 with no infrastructure.** |
| 3. Docs + CI | Claude session | 2 | `docs/EXA-API.md` (endpoints, auth, limits, schemas), `docs/EXA-PRICING.md`, `docs/ZERO-RETENTION.md`, `docs/PROVIDER-DATA-TERMS.md` draft (ToS 2.4), listing copy, `.github/workflows/tests.yml` running the unittest suite, `render.yaml` second service block (uncommitted until Brijesh reviews). |
| 4. Deploy | Brijesh | 2 | Create the Render service from `render.yaml` (Starter plan, `buildCommand: pip install -r api/requirements.txt && python scripts/build_exa_index.py && python -m unittest discover -s tests`, `startCommand: uvicorn api.exa_api:app --host 0.0.0.0 --port $PORT --no-access-log`), set `API_KEYS`, confirm Render workspace tier / HTTP logs, optional custom domain (`api.newsreel.co` or `perspectives-api.newsreel.co`) with Cloudflare logging off, add the new service id to the workflow's Render deploy step (a second curl) or leave it on auto-deploy from `main`. No Supabase or schema changes. |
| 5. Legal + reply | Jack + counsel | 1 (+ counsel) | Section 9 checklist; reply to David with section 7 facts and section 6 questions; sign the Marketplace Terms only after the ZDR Addendum/MSA are read. |
| 6. Optional MCP | Claude session | 4 | `api/mcp_server.py` (streamable HTTP, three tools over the same serializer) only if Exa requires MCP. |
| 7. Optional hygiene | Brijesh | 3 | Close the public `text` leaks noted in section 9 item 15 (block `/data/posts/` in `serve.py`; tighten the anon RLS policy or drop `text` from the anon-visible columns). Not required for Exa but recommended before the listing goes live. |

Claude-session total: ~12 h (16 with MCP). Brijesh: ~2 h (5 with hygiene). Nothing in Phases 1–3 touches secrets, Supabase, Render, or DNS.

---

## 9. Lawyer checklist

1. **Exa Connect ToS 4.6 (Rights in Provider Data):** we warrant we "have and will maintain all rights, licenses, consents, permissions, and authority" to submit the index and grant the 4.2 license, and that use "will not infringe … or breach any obligation of confidentiality, contractual restriction, or other legal obligation owed to any third party". Read against the platform terms in items 11–13.
2. **4.7 (Customer Data Restrictions):** the ZDR obligation; note Exa "may disclose to Provider the name and domain of a Customer" and we may not pass that on; references a Zero Data Retention Addendum to the MSA that we do not have. Request both.
3. **4.8 (Personal Data in Customer Data):** requires a public-facing privacy notice covering API processing, linked from our Provider Data Terms. Confirm newsreel.co's privacy policy covers it or draft an API addendum.
4. **4.9 (Personal Data in Provider Data):** "Provider Data shall not include any Personal Data. However, to the extent…" we warrant (a) lawful collection with consents and (b) no data about minors, sensitive data, or data "obtained through deceptive practices, unauthorized access, or in violation of any applicable laws or terms of service". Names of public figures plus their public statements are personal data under CCPA/GDPR definitions. This is the clause that meets the X guest-token method and the Instagram burner account. (Note: the "deceptive practices" language is in 4.9, not 4.7.)
5. **4.5 (Data Specifications):** accuracy and "not misleading" warranty plus a duty to "promptly correct any material errors". Applies to Haiku-generated `summary` and `stance` labels attributed to named people (defamation exposure: e.g. "Calls the strikes an illegal war"). Keep an "AI-generated label from the linked post" disclosure in the docs and the correction path in section 5.
6. **2.2 / 2.4 / 2.8:** Exa may "modify, reformat, summarize, excerpt" our data; we must publish Provider Data Terms for customers; listing changes need one week's notice and are at Exa's discretion. Draft the Provider Data Terms (attribution, no re-hosting, link-back, removal contact).
7. **4.2:** confirm the no-training clause and "transiently cache" wording are acceptable; ask for the cache TTL.
8. **7.2 / 7.3:** our indemnity for IP, privacy and "data collection law" claims, capped at 3× fees paid in the prior 12 months except gross negligence/fraud/wilful misconduct; general cap 2× fees. Confirm the cap reading and that it applies to 7.3.
9. **8.2 / 8.4:** 60-day termination for convenience with a duty to keep serving during the notice period; accrued payments within 30 days.
10. **3.3 / 3.4 / 9.1:** payout methodology and terms are "mutually agreed … in writing" (get the standard form); California law, San Francisco courts; the Service Terms' arbitration clause is superseded on conflict.
11. **X Terms of Service:** the scraping/crawling prohibition and the Liquidated Damages clause ($15,000 per 1,000,000 posts "requested, viewed, or accessed" in any 24-hour period; x.com/en/tos). Our collection: logged-out GraphQL with a guest token and X's public web bearer (`scripts/x_guest.py:1-23, 42-43, 227-263`), ~15,700 tweet objects per run, at most four runs/day (~64,000/day, well under the trigger). No account, no click-through acceptance in the collector itself, but Newsreel does hold X accounts (Jack's, @ragetrack) which may bind the company to the terms. Counsel to assess the browsewrap/hiQ posture, the X v. Bright Data history (dismissed May 2024 on copyright preemption, partially revived Nov 2024 on the server-access theory, settled June 2025), and whether serving derived positions (not text) changes it. X rows are 60% of the index.
12. **Instagram:** `scripts/collect.py:663-676` loads session cookies from "Chrome Profile 1 (burner account)" via `browser_cookie3` and calls `instagram.com/api/v1/users/web_profile_info` and `/api/v1/feed/user/` (lines 700-711). This is logged-in automated access under Meta's Terms; Meta v. Bright Data (Jan 2024) turned on logged-out access. It runs only on Jack's Mac (`data/collection-baseline.json:13`); the index holds 8 Instagram documents. Jack's decision Sep 14: collection stays; counsel decides whether the 8 rows are served (one-token switch in `SERVED_PLATFORMS`).
13. **YouTube / TikTok:** transcripts via `youtube-transcript-api` and `yt-dlp` (`collect.py:376-517`), TikTok enumeration via `yt-dlp` and captions via the official oEmbed (`collect.py:744-805`). Only titles/≤25-word excerpts are served.
14. **Quotes and labels as a commercial product:** ≤25-word attributed excerpt + paraphrased label + link; confirm fair use, hot-news/misappropriation, and right-of-publicity posture for resale to AI agents, and whether the `lens` editorial descriptor about a named person needs review.
15. **Existing exposure the "full text stays internal" claim must not contradict:** `serve.py:1061-1063` falls through to `SimpleHTTPRequestHandler` over the repo root, so `data/posts/<voice>/<date>.json` (full `text`) is publicly downloadable today; Supabase RLS lets the anon key read `posts.text` up to 5,000 chars (`SUPABASE-SCHEMA.sql:444-445`, `migrate_to_supabase.py:237`). Decide whether to close these before listing (Phase 7).

---

## 10. Risks and open decisions for Jack

- Jack pitched Exa "a corpus of original journalism from verified independent reporters" (Sep 9 email); this plan lists Perspectives. Decide: reframe to Perspectives, or list two providers (Perspectives now, contributor corpus later).
- Revenue is small: at $0.03/call before Exa's cut, $100K/yr needs ~280,000 calls/month; the contributor corpus was sized under $1K/yr. Do it for the deck line and the reusable endpoint.
- Render free tier cold starts would make Exa calls fail; the API needs a paid instance (Brijesh, ~$7/mo).
- If Exa requires MCP, add Phase 6 (~4 h) before the listing.
- 60% of the index comes from X guest-token collection; if X rotates its GraphQL query ids the index stalls at Bluesky/YouTube/TikTok (see `x_guest.py:45-49`).
- Haiku-written summaries and stance labels are warranted "accurate" under ToS 4.5 and attributed to named people; a disclosure line and a fast correction path are required.
- Instagram default "included" is a no-op today (8 documents); counsel may still ask for the switch.
- The public site and Supabase already expose full `text`; the Exa endpoint's never-serve-`text` rule is only as credible as those two doors.
- Do not quote the Supabase "209K posts" figure; it counts re-scrapes (8× duplication locally).
- Price: $0.03 (recommended) vs $0.02; single price vs per-tool.
- Takedown contact: `perspectives@newsreel.co` (new alias) vs `jack@newsreel.co`.
- The public site's own search logs queries and writes `data/results/` files; out of scope, but keep the Exa service separate so the ZDR statement stays narrowly and literally true.
- Exa's take rate, payout timing, and price-change rules are all "mutually agreed" in the ToS; nothing is standard until David sends the order form.

---

## Listing copy (draft)

**Newsreel Perspectives — where named voices stand.** A daily-refreshed citation index of positions taken by 300+ politicians, journalists, commentators, economists and creators across X, Bluesky, YouTube, TikTok, Substack, blogs and podcasts, organized under 40 news topics. Each result is a stance label, a one-line summary, a date, a platform, a short attributed excerpt and the canonical link to the original post. Built to send agents to the source, not to replace it. Full text is never served.

---

## Critical files for implementation

- `scripts/build_stances.py` — shape and filters of the durable stance store the index is built from (`stance_from_post` lines 81-98, no age-out 34-38, `looks_like_promo` 67-78).
- `scripts/collect.py` — per-post record contract and the derived fields (`_apply_categorization` 1148-1199, `_quote_from_text` 1032-1038, taxonomy enforcement 904-919, Instagram 661-739).
- `scripts/lookup.py` — local, LLM-free topic mapping to reuse for `q` (`_keyword_match` 297-370, `drop_unnamed_broad_topics` 112-116) and the query-persistence paths to avoid (232-295, 970-977).
- `serve.py` — existing rate-limit and mtime-cache patterns to mirror (122-136, 396-419), and the query logging / static-file behaviour the Exa service must not inherit (718-720, 843, 862, 1061-1063).
- `render.yaml` and `.github/workflows/daily-pipeline.yml` — where the second Render service, the build-time index step, and a CI test step get added.
