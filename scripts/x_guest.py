#!/usr/bin/env python3
"""
X/Twitter collection via guest tokens — the replacement for dead Nitter.

Nitter shut down on 2026-08-19 and took X collection with it (1,614 posts/day
to zero, 109 voices stranded). Every public mirror, syndication.twitter.com,
and twikit's guest flow are gone. But the mechanism Nitter itself used still
works when driven directly: activate a guest token against the public web
bearer, then call the same GraphQL endpoints the logged-out web client calls.

Two things made this look broken when it was not:
  - The handle used for testing (SenJohnThune) is a SUSPENDED account, so every
    probe returned UserUnavailable and read as "guests are blocked."
  - twikit's own token flow fails on X's current anti-bot, which is a library
    problem, not a platform one.

Rate limits are the real constraint, so this reuses one token across the whole
run, rotates on 429, and caches each handle's numeric id to disk so the
per-voice cost after the first run is a single request.

  python scripts/x_guest.py <handle>        # smoke-test one handle
  python scripts/x_guest.py --selftest      # a few handles + limit behaviour
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
ID_CACHE_PATH = ROOT / "data" / "x-user-ids.json"

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

# The public bearer the logged-out x.com web client ships with. Not a secret
# and not tied to any account — it is embedded in the site's JS bundle.
BEARER = ('AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D'
          '1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA')

# GraphQL query ids. These rotate when X ships a new bundle; if both endpoints
# start returning 404 rather than data, that is the thing to refresh (read them
# out of the main.*.js bundle on x.com).
Q_USER_BY_SCREEN_NAME = 'sLVLhk0bGj3MVFEKTdax1w'
Q_USER_TWEETS = 'V7H0Ap3_Hh2FyS75OCDO3Q'

USER_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

TWEET_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class XGuestClient:
    """Logged-out X reader. One token is shared across a whole run."""

    def __init__(self, verbose=False):
        self._token = None
        self._verbose = verbose
        self._ids = {}
        self.stats = {'tokens': 0, 'rate_limited': 0, 'suspended': 0,
                      'not_found': 0, 'ok': 0, 'errors': 0}
        if ID_CACHE_PATH.exists():
            try:
                self._ids = json.loads(ID_CACHE_PATH.read_text())
            except Exception:
                self._ids = {}

    # ── plumbing ──

    def _log(self, msg):
        if self._verbose:
            print(f"    [x] {msg}")

    def _new_token(self):
        req = urllib.request.Request(
            'https://api.twitter.com/1.1/guest/activate.json', data=b'',
            headers={'Authorization': f'Bearer {BEARER}', 'User-Agent': UA},
            method='POST')
        with urllib.request.urlopen(req, timeout=20) as r:
            self._token = json.loads(r.read().decode())['guest_token']
        self.stats['tokens'] += 1
        self._log(f"new guest token ({self.stats['tokens']} this run)")
        return self._token

    def _headers(self):
        if not self._token:
            self._new_token()
        return {
            'Authorization': f'Bearer {BEARER}',
            'x-guest-token': self._token,
            'User-Agent': UA,
            'x-twitter-active-user': 'yes',
            'x-twitter-client-language': 'en',
            'Content-Type': 'application/json',
            'Accept': '*/*',
        }

    def _gql(self, query_id, name, variables, features):
        """GET a GraphQL endpoint, rotating the token once on a rate limit."""
        params = urllib.parse.urlencode({
            'variables': json.dumps(variables),
            'features': json.dumps(features),
        })
        url = f'https://api.twitter.com/graphql/{query_id}/{name}?{params}'
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 403) and attempt == 1:
                    # A guest token has a request budget; take a fresh one.
                    self.stats['rate_limited'] += 1
                    self._log(f"{e.code} — rotating token")
                    self._token = None
                    time.sleep(2)
                    continue
                self.stats['errors'] += 1
                self._log(f"{name} HTTP {e.code}")
                return None
            except Exception as e:
                self.stats['errors'] += 1
                self._log(f"{name} {type(e).__name__}: {str(e)[:50]}")
                return None
        return None

    # ── public ──

    def user_id(self, handle):
        """Numeric id for a handle, cached to disk across runs.

        Returns None when the account is suspended, deleted or protected —
        which is a real answer, not a failure: SenJohnThune is suspended, and
        mistaking that for "guest access is blocked" is what made X collection
        look unfixable.
        """
        handle = handle.lstrip('@')
        key = handle.lower()
        if key in self._ids:
            return self._ids[key] or None

        data = self._gql(Q_USER_BY_SCREEN_NAME, 'UserByScreenName',
                         {"screen_name": handle, "withSafetyModeUserFields": True},
                         USER_FEATURES)
        if not data:
            return None
        result = (data.get('data') or {}).get('user', {}).get('result') or {}
        kind = result.get('__typename')
        if kind == 'User' and result.get('rest_id'):
            self._ids[key] = result['rest_id']
            return result['rest_id']

        msg = str(result.get('message', ''))
        if 'suspend' in msg.lower():
            self.stats['suspended'] += 1
        else:
            self.stats['not_found'] += 1
        self._log(f"@{handle}: {kind} {msg[:40]}")
        self._ids[key] = None       # negative-cache; do not re-ask every run
        return None

    def user_tweets(self, user_id, count=20):
        """Raw Tweet objects from a user's timeline."""
        data = self._gql(Q_USER_TWEETS, 'UserTweets', {
            "userId": user_id, "count": count, "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": False,
            "withVoice": True, "withV2Timeline": True,
        }, TWEET_FEATURES)
        if not data:
            return []

        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get('__typename') == 'Tweet' and 'legacy' in node:
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        self.stats['ok'] += 1
        return found

    def recent_posts(self, handle, count=20, own_only=True):
        """Normalized posts for a handle, newest first.

        own_only drops retweets and the replies-by-others that X threads into
        a profile timeline — attributing those to this voice would be exactly
        the misattribution the rest of the pipeline works to avoid.
        """
        uid = self.user_id(handle)
        if not uid:
            return []
        out = []
        for t in self.user_tweets(uid, count=count):
            lg = t.get('legacy') or {}
            if own_only and lg.get('user_id_str') != uid:
                continue
            if own_only and lg.get('retweeted_status_result'):
                continue
            text = lg.get('full_text') or ''
            # Long posts live under note_tweet, truncated in full_text.
            note = (((t.get('note_tweet') or {}).get('note_tweet_results') or {})
                    .get('result') or {}).get('text')
            if note:
                text = note
            if not text:
                continue
            try:
                when = datetime.strptime(lg['created_at'], '%a %b %d %H:%M:%S %z %Y')
            except Exception:
                when = datetime.now(timezone.utc)
            out.append({
                'id': t.get('rest_id') or lg.get('id_str'),
                'text': text,
                'created_at': when,
                'url': f"https://x.com/{handle.lstrip('@')}/status/{t.get('rest_id') or lg.get('id_str')}",
            })
        out.sort(key=lambda p: p['created_at'], reverse=True)
        return out

    def save_cache(self):
        try:
            ID_CACHE_PATH.write_text(json.dumps(self._ids, indent=2, sort_keys=True))
        except Exception:
            pass


def _selftest():
    client = XGuestClient(verbose=True)
    handles = ['elonmusk', 'JDVance', 'RonDeSantis', 'jaketapper',
               'SenJohnThune', 'seanhannity', 'VP', 'fareedzakaria']
    now = datetime.now(timezone.utc)
    ok = 0
    for h in handles:
        posts = client.recent_posts(h, count=20)
        if posts:
            ok += 1
            fresh = sum(1 for p in posts if (now - p['created_at']).days <= 7)
            print(f"  {h:<16} {len(posts):>3} posts, {fresh} in last 7d  "
                  f"| newest: {posts[0]['text'][:48]!r}")
        else:
            print(f"  {h:<16} none")
        time.sleep(1.2)
    client.save_cache()
    print(f"\n  {ok}/{len(handles)} handles returned posts")
    print(f"  stats: {client.stats}")


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _selftest()
    elif len(sys.argv) > 1:
        c = XGuestClient(verbose=True)
        for p in c.recent_posts(sys.argv[1], count=20)[:10]:
            print(f"  {p['created_at']:%Y-%m-%d %H:%M}  {p['text'][:70]!r}")
        c.save_cache()
    else:
        print(__doc__)
