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
