#!/usr/bin/env python3
"""Simple local server for testing the Perspectives viewer."""

import http.server
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get('PORT', 8888))
ROOT = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(ROOT, 'data', 'stories')
RESULTS_DIR = os.path.join(ROOT, 'data', 'results')


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        # Serve search.html as homepage
        if self.path == '/' or self.path.startswith('/?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'search.html')).read()
            self.wfile.write(html.encode())
            return

        # API: lookup story
        if self.path.startswith('/api/lookup?'):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            headline = params.get('q', [''])[0]

            days = params.get('days', [None])[0]

            if not headline:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing ?q= parameter'}).encode())
                return

            # Run lookup.py and capture JSON output
            try:
                cmd = [sys.executable, os.path.join(ROOT, 'scripts', 'lookup.py')]
                if days:
                    cmd.extend(['--days', str(int(days))])
                cmd.append(headline)
                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=60, cwd=ROOT
                )
                # Find the saved result file
                slug = re.sub(r'[^a-z0-9]+', '-', headline.lower())[:50]
                result_path = os.path.join(RESULTS_DIR, f'{slug}.json')
                if os.path.exists(result_path):
                    data = open(result_path).read()
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(data.encode())
                else:
                    self.send_response(404)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': 'No results found', 'stderr': result.stderr}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # API: list topics
        if self.path == '/api/topics':
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            index_files = sorted(
                [f for f in os.listdir(posts_dir) if f.startswith('topic-index-')],
                reverse=True
            )
            if index_files:
                data = open(os.path.join(posts_dir, index_files[0])).read()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data.encode())
            else:
                self.send_response(404)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')
            return

        # API: unified stories feed (new system)
        if self.path == '/api/stories':
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            story_files = sorted(
                [f for f in os.listdir(posts_dir) if f.startswith('stories-')],
                reverse=True
            )
            if story_files:
                data = open(os.path.join(posts_dir, story_files[0])).read()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data.encode())
            else:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'[]')
            return

        # API: fractures (backward compat — also served by /api/stories)
        if self.path == '/api/fractures':
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            fracture_files = sorted(
                [f for f in os.listdir(posts_dir) if f.startswith('fractures-')],
                reverse=True
            )
            if fracture_files:
                data = open(os.path.join(posts_dir, fracture_files[0])).read()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data.encode())
            else:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'[]')
            return

        # API: wire — today's voice activity feed
        if self.path == '/api/wire':
            from datetime import date
            today = date.today().isoformat()
            posts_dir = os.path.join(ROOT, 'data', 'posts')
            voices_path = os.path.join(ROOT, 'data', 'voices.json')

            # Load voice metadata
            voice_meta = {}
            try:
                with open(voices_path) as f:
                    for v in json.loads(f.read()):
                        voice_meta[v['id']] = v
            except Exception:
                pass

            all_posts = []
            try:
                for voice_dir in os.listdir(posts_dir):
                    day_file = os.path.join(posts_dir, voice_dir, f'{today}.json')
                    if not os.path.isfile(day_file):
                        continue
                    try:
                        with open(day_file) as f:
                            data = json.loads(f.read())
                    except Exception:
                        continue
                    posts = data.get('posts', []) if isinstance(data, dict) else data
                    meta = voice_meta.get(voice_dir, {})
                    for p in posts:
                        text = (p.get('text') or '').strip()
                        if len(text) < 30:
                            continue
                        all_posts.append({
                            'voiceId': voice_dir,
                            'voiceName': meta.get('name', p.get('voiceName', voice_dir)),
                            'photo': meta.get('photo', ''),
                            'platform': p.get('platform', ''),
                            'text': text[:200],
                            'sourceUrl': p.get('sourceUrl', ''),
                            'timestamp': p.get('timestamp', ''),
                        })
            except Exception:
                pass

            all_posts.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            all_posts = all_posts[:100]

            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(all_posts).encode())
            return

        # Serve cached voice photos
        if self.path.startswith('/photos/'):
            filename = self.path.split('/photos/')[1]
            photo_path = os.path.join(ROOT, 'data', 'photos', filename)
            if os.path.exists(photo_path):
                content_type = 'image/png' if filename.endswith('.png') else 'image/jpeg'
                self.send_response(200)
                self.send_header('Content-type', content_type)
                self.send_header('Cache-Control', 'public, max-age=604800')  # 7 day cache
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                with open(photo_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'Photo not found')
            return

        # Serve search page
        if self.path == '/search' or self.path.startswith('/search?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'search.html')).read()
            self.wfile.write(html.encode())
            return

        # Serve voice profile page
        if self.path.startswith('/voice/'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'voice.html')).read()
            self.wfile.write(html.encode())
            return

        # Serve story permalink page
        if self.path.startswith('/story/'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'story.html')).read()
            self.wfile.write(html.encode())
            return

        # Serve editorial review dashboard
        if self.path == '/review' or self.path.startswith('/review?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'review.html')).read()
            self.wfile.write(html.encode())
            return

        # Serve voices dashboard
        if self.path == '/voices' or self.path.startswith('/voices?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'voices.html')).read()
            self.wfile.write(html.encode())
            return

        # Serve methodology page
        if self.path == '/methodology' or self.path.startswith('/methodology?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'methodology.html')).read()
            self.wfile.write(html.encode())
            return

        return super().do_GET()

    def do_POST(self):
        # API: save editorial review
        if self.path == '/api/review':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            try:
                review = json.loads(body)
                reviews_path = os.path.join(ROOT, 'data', 'editorial-reviews.json')
                existing = []
                if os.path.exists(reviews_path):
                    with open(reviews_path) as f:
                        existing = json.loads(f.read())
                existing.append(review)
                with open(reviews_path, 'w') as f:
                    f.write(json.dumps(existing, indent=2))

                # If review contains overrides, save them for pipeline use
                overrides_path = os.path.join(ROOT, 'data', 'editorial-overrides.json')
                overrides = {}
                if os.path.exists(overrides_path):
                    with open(overrides_path) as f:
                        overrides = json.loads(f.read())
                if review.get('overrides'):
                    headline = review.get('headline', '')
                    if headline:
                        overrides[headline] = review['overrides']
                        with open(overrides_path, 'w') as f:
                            f.write(json.dumps(overrides, indent=2))

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return


if __name__ == '__main__':
    print(f'\n  Perspectives Viewer running at http://localhost:{PORT}')
    print(f'  Stories dir: {STORIES_DIR}')
    print(f'  Press Ctrl+C to stop\n')

    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')
