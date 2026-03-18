from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from urllib.parse import unquote

from pralph.models import StoryStatus
from pralph.report import gather_report_data
from pralph.state import StateManager

_VIEWER_HTML_PATH = Path(__file__).with_name("viewer.html")


class ViewerHandler(BaseHTTPRequestHandler):
    state: StateManager

    def do_GET(self):
        self.state.refresh_readonly()
        if self.path == '/api/stories':
            self._serve_stories()
        elif self.path == '/api/status':
            self._serve_status()
        elif self.path == '/api/tokens':
            self._serve_tokens()
        elif self.path == '/api/report':
            self._serve_report()
        elif self.path == '/api/phases':
            self._serve_phases()
        elif self.path == '/api/projects':
            self._serve_projects()
        elif self.path == '/api/solutions':
            self._serve_solutions()
        elif self.path.startswith('/api/solutions/'):
            self._serve_solution_detail()
        elif self.path == '/api/run-log':
            self._serve_run_log()
        elif self.path == '/api/settings':
            self._serve_settings()
        else:
            self._serve_html()

    def _serve_html(self):
        body = _VIEWER_HTML_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stories(self):
        stories = self.state.load_stories()
        data = [s.to_dict() for s in stories]
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        entries = self.state.load_status_log()
        body = json.dumps(entries, default=str).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_tokens(self):
        data = self.state.get_story_tokens()
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_report(self):
        data = gather_report_data(self.state)
        self._json_response(data)

    def _serve_phases(self):
        rows = self.state.load_all_phase_states()
        self._json_response(rows)

    def _serve_projects(self):
        try:
            from pralph import db
            conn = db.get_readonly_connection()
            try:
                result = conn.execute(
                    "SELECT project_id, name, created_at FROM projects ORDER BY created_at DESC"
                )
                cols = [d[0] for d in result.description]
                rows = [dict(zip(cols, r)) for r in result.fetchall()]
            finally:
                conn.close()
        except Exception:
            # DuckDB not available or no data — return just the current project
            rows = [{"project_id": self.state.project_id, "name": self.state.project_id, "created_at": ""}]
        self._json_response(rows)

    def _serve_solutions(self):
        entries = self.state.load_solutions_index()
        self._json_response(entries)

    def _serve_solution_detail(self):
        filename = unquote(self.path[len('/api/solutions/'):])
        content = self.state.read_solution(filename)
        if not content:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = content.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/markdown; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_run_log(self):
        entries = self.state.load_run_log()
        # Round values and take last 50 (newest first)
        for e in entries:
            if "duration" in e:
                e["duration"] = round(e["duration"], 1)
            if "cost_usd" in e:
                e["cost_usd"] = round(e["cost_usd"], 4)
        rows = list(reversed(entries[-50:]))
        self._json_response(rows)

    def _serve_settings(self):
        settings = {"project_id": self.state.project_id, "project_dir": str(self.state.project_dir)}
        claude_settings_path = Path.home() / ".claude" / "settings.json"
        if claude_settings_path.exists():
            try:
                claude_data = json.loads(claude_settings_path.read_text())
                env = claude_data.get("env", {})
                settings["models"] = {
                    "opus": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
                    "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
                    "haiku": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
                }
                settings["model"] = claude_data.get("model", "")
            except (json.JSONDecodeError, OSError):
                pass
        self._json_response(settings)

    def _json_response(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self.path.startswith('/api/stories/'):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        story_id = unquote(self.path[len('/api/stories/'):])

        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        stories = self.state.load_stories()
        story = next((s for s in stories if s.id == story_id), None)
        if not story:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        editable = ('title', 'content', 'acceptance_criteria', 'priority',
                     'category', 'complexity', 'dependencies', 'status')
        for field in editable:
            if field in body:
                if field == 'status':
                    story.status = StoryStatus(body[field])
                else:
                    setattr(story, field, body[field])

        try:
            self.state._rewrite_stories(stories)
        except Exception:
            msg = b'{"error": "Database is locked by another pralph process. Try again later."}'
            self.send_response(HTTPStatus.CONFLICT)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        resp = json.dumps(story.to_dict()).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format, *args):
        # Suppress default request logging
        pass


def run_viewer(state: StateManager, *, port: int = 8411, open_browser: bool = True) -> None:
    """Start the viewer HTTP server."""
    handler = type('Handler', (ViewerHandler,), {'state': state})
    server = HTTPServer(('127.0.0.1', port), handler)
    url = f'http://127.0.0.1:{port}'

    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()

    print(f'pralph viewer running at {url}')
    print('Press Ctrl+C to stop')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping viewer')
        server.shutdown()
