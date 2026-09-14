# Perspectives data flow: the ideal shape, and the AI-company door

**Date:** 2026-09-14
**Status:** DRAFT map for Jack + Brijesh. Nothing built. The Exa endpoint detail lives in `EXA-CONNECT-PLAN.md`.
**Visual version:** https://claude.ai/code/artifact/746fd30f-d47f-4b01-85ed-590bab508e70

One database. One public view. One door. Every AI company plugs into the same door, and the door has no text in it.

## The idea

Today Perspectives is a set of daily snapshot files. The truth lives in three places at once (day files, the stance store, Supabase rows keyed by post and day), the same post gets labeled and stored again every day it stays in a feed's top 20, and the raw post text sits in the open. There is no consent flag and no takedown list. Nothing outside Newsreel can query it.

The ideal version keeps the collectors exactly as they are and changes everything after them. Posts get deduplicated on the way in, labeled once, and stored one row per post in one canonical table. A public view strips the text and applies the rights rules. One API sits on that view. The Perspectives site, the app, the newsletter, Exa, Microsoft, AskNews, and any agent framework read through that same API with their own key. Later, opted-in contributor articles ride the same rails as a second tier, full text included, because for those we hold the rights.

## The map

```
MAKING THE DATA (internal)
  SOURCES ──latest 20 per feed──▶ INGEST ──new URLs only──▶ RAW STORE ──once per post──▶ LABEL ONCE ──labels + version──▶ CANONICAL DB
  X (guest, no login)             cursor per voice          posts_raw                    Haiku 4.5                         voices, topics
  Bluesky (public API)            dedupe by URL             text (internal only)         topic, stance                     positions (one row per post)
  YouTube, TikTok                 new posts only            published_at                 summary, quote                    articles (consented)  ◀── Newsreel Verified contributors, later:
  Substack/blogs/podcasts (RSS)   4 runs a day              first_seen, last_seen        relevance                         consent, suppressions      opt-in per story, rights held, 50/50 payout
  Instagram (Jack's Mac)                                    never served                 model_version                     api_keys, usage

════════════════════ SERVING BOUNDARY: post text never crosses ════════════════════ (allowlisted fields only ▼)

SELLING THE DATA (external)
  MONEY ◀──counts── ONE DOOR ◀──reads── PUBLIC VIEW
  usage per key/day (counts only)      REST /v1 · MCP · bulk JSONL · webhooks     positions_public: allowlisted fields, no text,
  marketplaces pay us out              one key per partner, rotatable              suppressions + platform allowlist, quote ≤ 25 words
  direct deals via Stripe              partner keys: zero retention, no query logs articles_licensed: consented full text only
  articles: 50/50 to the writer        site key: analytics allowed, kept separate
                                       │
                 ┌─────────────────────┼──────────────────────┬────────────────────────┐   same API, same keys, same fields
        NEWSREEL SURFACES      MARKETPLACES (per call)   DIRECT DEALS             AGENTS (MCP)
        Perspectives site      Exa Connect ($0.03)       AskNews (signed Jun 1)   Claude, ChatGPT, Cursor
        App: Where they stand  Microsoft PCM             labs + answer engines    search_positions
        Daily Stack module     (TollBit/Cloudflare/      bulk feed, rights        get_voice
                                ProRata = articles tier)  statement, provenance    list_topics
```

Data is made above the line and sold below it. The public view is the only path across, and it has no text column. Contributor articles are the one exception: consented, rights held, sold as a separate tier through the same door.

## What changes from today

| Area | Today | Ideal |
|---|---|---|
| Memory across runs | None. Every run re-pulls the last 20 posts per feed and writes all of them to the day's file. | A cursor per voice and a seen set. Only new URLs move forward. Feeds and the 4 runs a day stay the same. |
| Labeling | Haiku labels every appearance. A post seen on 5 days costs 5 calls. | One call per URL. Labels carry a model version, so re-labeling is a decision, not an accident. |
| Source of truth | Three copies: day files, stance store, Supabase rows keyed by post and day (about 8x inflated). | One `positions` table, one row per post, first_seen and last_seen. Day files become a cache. |
| Post text | In the open: the site serves raw day files as static files; the Supabase anon key can read `text`. | Internal column. The public view has no text, the anon key cannot read it, the static fallthrough is closed. |
| Rights | No consent flag, no takedown list. | `consent` and `suppressions` tables, checked at serve time. A voice is in or out by a flag. |
| Serving | Site reads files. Search sends queries to Claude, logs them, writes result files. | One API, one key per partner. Partner keys never log a query. The site keeps its own key and analytics. |
| Counting | "209K posts." | About 44,000 positions, 326 voices, 450 to 550 new a day. |
| Distribution | Nothing outside Newsreel can query it. | Marketplaces, direct deals, agent frameworks, our own surfaces, all through the same door. |

## AI companies: which door they use

Two tiers come out of the same API. The **index** is the derived layer (who said what, where they stand, a short quote, a link); it needs no consent because it is facts about public statements, and it honors takedowns. **Articles** are consented full text from contributors and only exist once the consent flag and payout ledger are built.

| Channel | Fits | What they need from us | How money moves | Status |
|---|---|---|---|---|
| Exa Connect | Index | REST or MCP docs, pricing sheet, zero-retention statement, corpus size + growth | Per call, $0.03 proposed; Exa's cut not in their terms | In talks; David's 5 questions open; `EXA-CONNECT-PLAN.md` |
| Microsoft PCM | Index, later articles | A listed feed and a rate on the Publisher Content Marketplace | Pay per use through Microsoft | Existing channel via Matt Jubelirer; nothing listed |
| AskNews | Index, later articles | A feed they can poll, attribution rules | Signed licence | Signed Jun 1; the reference customer |
| Labs and answer engines | Index, later articles | Bulk feed (daily JSONL), rights statement per source class, provenance fields (SPUR) | Negotiated: annual or per retrieval | No contact; the deck line |
| Agent builders | Index | MCP server, three tools, a key | Per call or free discovery tier | Not built; ~4h after the API |
| ProRata / Gist | Articles only | Page content with attribution in answers | 50/50 pool, $1,000 payout floor | MSA reviewed; needs contributor consent; no buyers yet |
| TollBit, Cloudflare pay-per-crawl | Articles only | A crawl gate on newsreel.co pages | Per crawl | Cloudflare needs Brijesh; nothing enrolled |

The crawl-metered rails want content behind a gate. The index is not content, it is a table. So the rails split cleanly: API marketplaces and direct deals get the index now; the crawl rails wait for the articles tier.

## Build order

| | Phase | What | Owner, effort |
|---|---|---|---|
| A | Stop the waste | Label a post once (look the URL up in the stance store before Haiku). Sync only today's files to Supabase; stop asking it to echo rows back. No schema change. | Claude in repo, ~4h |
| B | One truth | Key `positions` by post with first_seen/last_seen. Add `consent`, `suppressions`. Create `positions_public` with no text. Take `text` away from the anon key; close the static fallthrough in `serve.py`. | Brijesh, ~1 day; Supabase, permission first |
| C | One door | The API from the Exa plan: allowlist serializer, build-failing text test, bearer keys, rate limits, no access log. MCP wrapper after. Paid Render instance. | Claude ~10h, Brijesh ~2h |
| D | Partner kit | Daily JSONL bulk feed, rights statement per source class, provenance fields, pricing sheet, listing copy. List on Exa, list on PCM, point AskNews at the feed. | Claude ~6h, Jack for the conversations |
| E | Articles tier | Consent per story in the CMS, `articles` table, payout ledger splitting 50/50 to the writer. Only then ProRata, TollBit, Cloudflare. | Brijesh + legal, after the NYC pilot has writers publishing |

## Five rules

1. **One truth.** If two stores disagree, the positions table wins. Day files are a cache.
2. **Text never crosses the line.** The public view has no text column, and a test proves it on every deploy.
3. **Label once.** Re-label only when the model or the taxonomy changes, and record which.
4. **Every partner is a key.** Same fields, same limits, same zero-retention promise. No custom exports.
5. **Consent is a column.** A voice or a writer is in or out by a flag, not by a folder of contracts.

## Open source that fits (so we build less)

No open-source project builds a stance directory of public figures; that is the product, and it is why it is sellable. The plumbing around it exists:

- **Datasette** (simonw/datasette, Apache-2.0): turns a SQLite file of `positions_public` into a browsable directory and a read-only JSON API in one command; `datasette-auth-tokens` gives bearer keys per partner. Fastest way to a demo directory. Caveat for zero retention: its API is GET-based, so query text lands in URLs and in any host request log; fine for the site, weaker for partner keys unless the host's request logging is off.
- **Meilisearch** or **Typesense** (MIT / GPL-3): faceted search over the positions (voice, topic, stance, platform, date) with typo tolerance and search-only API keys; would replace hand-rolled ranking in the API.
- **PostgREST** (already inside Supabase): a REST endpoint on the `positions_public` view with zero new code; the trade is less control over logging and keys.
- **RSL, Really Simple Licensing** (rslstandard.org, open standard, RSL 1.0): machine-readable licensing terms in robots.txt, RSS, and HTTP headers, with pay-per-crawl and pay-per-inference terms. The right way to publish the "rights statement per source class" so crawlers and labs can read it.
- **Wikidata QIDs** on every voice: the entity key AI companies already use, so our 334 voices join to theirs without name matching.

## Counts (local data, Sep 14 2026)

44,259 positions; 326 voices; roughly 450 to 550 new a day (~15,000 a month). By platform: X 26,696, Bluesky 8,112, YouTube 6,926, TikTok 1,427, Substack 844, blog 210, podcast 36, Instagram 8. The Supabase row count is not a document count.
