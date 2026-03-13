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
        # Serve viewer.html with injected story manifest
        if self.path == '/' or self.path.startswith('/?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()

            html = open(os.path.join(ROOT, 'viewer.html')).read()

            # Get story files
            story_files = []
            if os.path.isdir(STORIES_DIR):
                story_files = sorted(
                    [f for f in os.listdir(STORIES_DIR) if f.endswith('.json')],
                    reverse=True
                )

            # Inject story file list
            manifest = f'const STORY_FILES = {json.dumps(story_files)};'
            html = html.replace(
                'const STORY_FILES = [];',
                manifest
            )

            self.wfile.write(html.encode())
            return

        # API: lookup story
        if self.path.startswith('/api/lookup?'):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            headline = params.get('q', [''])[0]

            if not headline:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing ?q= parameter'}).encode())
                return

            # Run lookup.py and capture JSON output
            try:
                result = subprocess.run(
                    [sys.executable, os.path.join(ROOT, 'scripts', 'lookup.py'), headline],
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

        # Serve search page
        if self.path == '/search' or self.path.startswith('/search?'):
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = open(os.path.join(ROOT, 'search.html')).read()
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

        return super().do_GET()


if __name__ == '__main__':
    print(f'\n  Perspectives Viewer running at http://localhost:{PORT}')
    print(f'  Stories dir: {STORIES_DIR}')
    print(f'  Press Ctrl+C to stop\n')

    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')
