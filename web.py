#!/usr/bin/env python3
"""
Lightweight web UI for nws-bout-indexer.
Run: python web.py
Open: http://localhost:9000
"""

import json
import os
import queue
import threading
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse


from bout_indexer import run_pipeline

PORT = 9000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# In-memory job store
jobs = {}  # job_id -> {status, queue, result, error}


class Handler(SimpleHTTPRequestHandler):
    """Handle API routes and serve static files."""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            self._serve_file("index.html", "text/html")
        elif path.startswith("/api/progress/"):
            job_id = path.split("/")[-1]
            self._stream_progress(job_id)
        elif path.startswith("/api/result/"):
            job_id = path.split("/")[-1]
            self._get_result(job_id)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            self._start_job(body)
        else:
            self.send_error(404)

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(SCRIPT_DIR, filename)
        if not os.path.exists(filepath):
            self.send_error(404)
            return
        with open(filepath, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _start_job(self, params):
        video_path = os.path.realpath(os.path.expanduser(params.get("video", "")))
        if not os.path.exists(video_path):
            self._json_response(400, {"error": f"File not found: {video_path}"})
            return

        video_dir = os.path.dirname(video_path)
        video_stem = Path(video_path).stem

        job_id = str(uuid.uuid4())[:8]
        q = queue.Queue()

        jobs[job_id] = {
            "status": "running",
            "queue": q,
            "result": None,
            "error": None,
        }

        options = {
            "video_path": video_path,
            "output_path": os.path.realpath(
                params.get("output") or os.path.join(video_dir, f"{video_stem} Chapters.txt")
            ),
            "ocr_dir": os.path.realpath(
                params.get("ocr_dir") or os.path.join(video_dir, f"{video_stem} OCR")
            ),
            "fps": float(params.get("fps", 1 / 3)),
            "include_lineups": params.get("include_lineups", True),
            "include_period_clock": params.get("include_period_clock", True),
            "team1": params.get("team1") or "Team 1",
            "team2": params.get("team2") or "Team 2",
            "roster1_path": params.get("roster1", ""),
            "roster2_path": params.get("roster2", ""),
            "reprocess": params.get("reprocess", False),
        }

        def worker():
            def progress_cb(phase, current, total, message):
                q.put({"phase": phase, "current": current, "total": total, "message": message})

            try:
                result = run_pipeline(progress_cb=progress_cb, **options)
                jobs[job_id]["result"] = result
                jobs[job_id]["status"] = "done"
                q.put({"phase": "done", "current": 1, "total": 1, "message": "Complete!"})
            except Exception as e:
                jobs[job_id]["error"] = str(e)
                jobs[job_id]["status"] = "error"
                q.put({"phase": "error", "current": 0, "total": 0, "message": str(e)})

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        self._json_response(200, {"job_id": job_id})

    def _stream_progress(self, job_id):
        if job_id not in jobs:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        job = jobs[job_id]
        while True:
            try:
                event = job["queue"].get(timeout=30)
                data = json.dumps(event)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                if event.get("phase") in ("done", "error"):
                    break
            except queue.Empty:
                # Keepalive
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def _get_result(self, job_id):
        if job_id not in jobs:
            self.send_error(404)
            return
        job = jobs[job_id]
        self._json_response(200, {
            "status": job["status"],
            "result": job["result"],
            "error": job["error"],
        })

    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default access logs to keep terminal clean
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"nws-bout-indexer web UI")
    print(f"  http://localhost:{PORT}")
    print(f"  Press Ctrl+C to stop")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
