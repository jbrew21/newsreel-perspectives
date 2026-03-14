#!/bin/bash
# Newsreel Perspectives — Daily Pipeline
# Runs collection, enrichment, fracture computation, and monitoring.
# Use this in cron instead of calling collect.py directly.
#
# Crontab entry:
#   0 6 * * * /Users/jackbrewster/Desktop/Newsreel_OS/06_product/perspectives/scripts/daily.sh >> /tmp/perspectives-daily.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="/opt/homebrew/bin/python3"
LOG="/tmp/perspectives-daily.log"
DATE=$(date +%Y-%m-%d)

# Ensure PATH includes homebrew and yt-dlp
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Source .env for API keys (ANTHROPIC_API_KEY, RENDER_API_KEY, etc.)
ENV_FILE="$PROJECT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# Slack webhook (set in .env or export before running)
SLACK_WEBHOOK="${PERSPECTIVES_SLACK_WEBHOOK:-}"

send_alert() {
  local msg="$1"
  local emoji="${2:-:warning:}"
  echo "  [ALERT] $msg"
  if [ -n "$SLACK_WEBHOOK" ]; then
    curl -s -X POST "$SLACK_WEBHOOK" \
      -H 'Content-type: application/json' \
      -d "{\"text\": \"${emoji} *Perspectives Daily* (${DATE})\\n${msg}\"}" > /dev/null 2>&1
  fi
}

send_success() {
  local msg="$1"
  echo "  [OK] $msg"
  if [ -n "$SLACK_WEBHOOK" ]; then
    curl -s -X POST "$SLACK_WEBHOOK" \
      -H 'Content-type: application/json' \
      -d "{\"text\": \":white_check_mark: *Perspectives Daily* (${DATE})\\n${msg}\"}" > /dev/null 2>&1
  fi
}

echo ""
echo "=========================================="
echo "  Perspectives Daily Pipeline"
echo "  Date: $DATE"
echo "  Started: $(date '+%H:%M:%S')"
echo "=========================================="

cd "$PROJECT_DIR"

# Phase 1: Collect posts
echo ""
echo "--- Phase 1: Collecting posts ---"
COLLECT_START=$(date +%s)
if $PYTHON scripts/collect.py 2>&1; then
  COLLECT_END=$(date +%s)
  COLLECT_TIME=$((COLLECT_END - COLLECT_START))
  echo "  Collection completed in ${COLLECT_TIME}s"
else
  send_alert "Collection FAILED. Check /tmp/perspectives-daily.log"
  exit 1
fi

# Phase 2: Enrich YouTube transcripts
echo ""
echo "--- Phase 2: Enriching transcripts ---"
if $PYTHON scripts/enrich_transcripts.py 2>&1; then
  echo "  Transcript enrichment completed"
else
  send_alert "Transcript enrichment failed (non-fatal)" ":yellow_circle:"
fi

# Phase 3: Build unified stories feed (CMS + voice-driven)
echo ""
echo "--- Phase 3: Building stories feed ---"
if $PYTHON scripts/stories.py 2>&1; then
  echo "  Stories feed built"
else
  # Fallback to old fractures.py if stories.py fails
  echo "  Stories failed, falling back to fractures.py..."
  if $PYTHON scripts/fractures.py 2>&1; then
    echo "  Fractures computed (fallback)"
  else
    send_alert "Both stories and fractures computation failed" ":yellow_circle:"
  fi
fi

# Phase 4: Health check
echo ""
echo "--- Phase 4: Health check ---"

# Check topic index exists
TOPIC_INDEX="$PROJECT_DIR/data/posts/topic-index-${DATE}.json"
if [ ! -f "$TOPIC_INDEX" ]; then
  send_alert "No topic index generated for $DATE"
  exit 1
fi

# Count posts and topics
STATS=$($PYTHON -c "
import json
d = json.load(open('$TOPIC_INDEX'))
topics = len(d)
posts = sum(len(v) for v in d.values())
voices = len(set(e['voiceId'] for entries in d.values() for e in entries))
uncategorized = len(d.get('uncategorized', []))
uncategorized_pct = round(uncategorized / max(posts, 1) * 100)
print(f'{posts}|{voices}|{topics}|{uncategorized_pct}')
")

POSTS=$(echo "$STATS" | cut -d'|' -f1)
VOICES=$(echo "$STATS" | cut -d'|' -f2)
TOPICS=$(echo "$STATS" | cut -d'|' -f3)
UNCAT_PCT=$(echo "$STATS" | cut -d'|' -f4)

echo "  Posts: $POSTS"
echo "  Voices: $VOICES"
echo "  Topics: $TOPICS"
echo "  Uncategorized: ${UNCAT_PCT}%"

# Alert on low post count
if [ "$POSTS" -lt 50 ]; then
  send_alert "Low post count: only $POSTS posts collected (expected 200+)"
elif [ "$UNCAT_PCT" -gt 50 ]; then
  send_alert "High uncategorized rate: ${UNCAT_PCT}% posts uncategorized (likely API key issue)"
fi

# Check stories/fractures
STORIES_FILE="$PROJECT_DIR/data/posts/stories-${DATE}.json"
FRACTURES="$PROJECT_DIR/data/posts/fractures-${DATE}.json"
STORY_COUNT=0
if [ -f "$STORIES_FILE" ]; then
  STORY_COUNT=$($PYTHON -c "import json; print(len(json.load(open('$STORIES_FILE'))))")
elif [ -f "$FRACTURES" ]; then
  STORY_COUNT=$($PYTHON -c "import json; print(len(json.load(open('$FRACTURES'))))")
fi

# Phase 5: Trigger Render deploy
echo ""
echo "--- Phase 5: Deploy ---"
RENDER_KEY="${RENDER_API_KEY:-}"
if [ -n "$RENDER_KEY" ]; then
  curl -s -X POST "https://api.render.com/v1/services/srv-d6pitsmuk2gs73fhkj70/deploys" \
    -H "Authorization: Bearer $RENDER_KEY" \
    -H "Content-Type: application/json" \
    -d '{}' > /dev/null 2>&1
  echo "  Render deploy triggered"
else
  echo "  No RENDER_API_KEY set, skipping deploy trigger"
fi

# Phase 6: Git commit and push (optional, auto-commit data)
echo ""
echo "--- Phase 6: Git push ---"
cd "$PROJECT_DIR"
if git diff --quiet data/posts/ 2>/dev/null; then
  echo "  No new data to commit"
else
  git add data/posts/
  git commit -m "Daily collection: $DATE — $POSTS posts, $VOICES voices, $TOPICS topics" --no-verify 2>/dev/null
  git push origin main 2>/dev/null && echo "  Pushed to GitHub" || echo "  Push failed (non-fatal)"
fi

# Final summary
END_TIME=$(date '+%H:%M:%S')
TOTAL_TIME=$(( $(date +%s) - COLLECT_START ))

send_success "$POSTS posts from $VOICES voices across $TOPICS topics. $STORY_COUNT stories. Took ${TOTAL_TIME}s."

echo ""
echo "=========================================="
echo "  Completed: $END_TIME (${TOTAL_TIME}s)"
echo "=========================================="
