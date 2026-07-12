"""Gateway HTTP com keepalive para solves maiores que o timeout da Cloudflare."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests


class GatewayHandler(BaseHTTPRequestHandler):
    upstream: str = "http://127.0.0.1:8777"
    solve_timeout: int = 300
    access_token: str = ""
    jobs: dict[str, dict[str, object]] = {}
    jobs_lock = threading.Lock()
    solve_slots = threading.BoundedSemaphore(1)
    job_ttl_seconds = 3600

    def log_message(self, fmt: str, *args) -> None:
        # Nunca registre a query string: ela pode carregar o token de acesso.
        path = urlsplit(self.path).path
        print(f"[gateway] {self.client_address[0]} {self.command} {path}", flush=True)

    def authorized(self) -> bool:
        if not self.access_token:
            return True
        query_token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        header_token = self.headers.get("X-Solver-Token", "")
        supplied = header_token or query_token
        return bool(supplied) and hmac.compare_digest(supplied, self.access_token)

    @classmethod
    def cleanup_jobs(cls) -> None:
        cutoff = time.time() - cls.job_ttl_seconds
        with cls.jobs_lock:
            expired = [job_id for job_id, job in cls.jobs.items() if float(job["created_at"]) < cutoff]
            for job_id in expired:
                cls.jobs.pop(job_id, None)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self.authorized():
            self.send_json(401, {"success": False, "reason": "unauthorized"})
            return
        path = urlsplit(self.path).path
        if path.startswith("/jobs/"):
            job_id = path.split("/jobs/", 1)[1]
            with self.jobs_lock:
                job = self.jobs.get(job_id)
            if not job:
                self.send_json(404, {"success": False, "reason": "job_not_found"})
                return
            if not job["finished"].is_set():
                self.send_json(202, {"accepted": True, "job_id": job_id, "status": "pending"})
                return
            body = bytes(job.get("body") or b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path != "/health":
            self.send_error(404)
            return
        try:
            response = requests.get(f"{self.upstream}/health", timeout=10)
            body = response.content
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except requests.RequestException as exc:
            self.send_error(503, str(exc))

    def do_POST(self) -> None:
        if not self.authorized():
            self.send_json(401, {"success": False, "reason": "unauthorized"})
            return
        if urlsplit(self.path).path != "/solve":
            self.send_error(404)
            return
        self.cleanup_jobs()
        if not self.solve_slots.acquire(blocking=False):
            self.send_json(429, {"success": False, "reason": "solver_busy"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length)
        job_id = uuid.uuid4().hex
        job: dict[str, object] = {
            "created_at": time.time(),
            "finished": threading.Event(),
            "body": b"",
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

        def call_upstream() -> None:
            try:
                response = requests.post(
                    f"{self.upstream}/solve",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.solve_timeout,
                )
                job["body"] = response.content
            except requests.RequestException as exc:
                job["body"] = json.dumps(
                    {"success": False, "reason": "upstream_error", "error": str(exc)},
                    ensure_ascii=False,
                ).encode("utf-8")
            finally:
                job["finished"].set()
                self.solve_slots.release()

        threading.Thread(target=call_upstream, daemon=True).start()
        self.send_json(202, {"accepted": True, "job_id": job_id, "status": "pending"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8778)
    parser.add_argument("--upstream", default="http://127.0.0.1:8777")
    parser.add_argument("--solve-timeout", type=int, default=300)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--job-ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    GatewayHandler.upstream = args.upstream.rstrip("/")
    GatewayHandler.solve_timeout = max(30, args.solve_timeout)
    GatewayHandler.access_token = os.environ.get("PORTAL_SOLVER_GATEWAY_TOKEN", "").strip()
    GatewayHandler.solve_slots = threading.BoundedSemaphore(max(1, args.max_concurrent))
    GatewayHandler.job_ttl_seconds = max(300, args.job_ttl_seconds)
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"gateway listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
