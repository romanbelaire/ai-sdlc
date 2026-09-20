"""Local team dashboard: backlog cards, feature requests, sessions and hub chat.

Standard library only. Serves one page on 127.0.0.1 and a small JSON API over
the shared hub database. The human writes to the hub as --user (default
"roman"): chat to one or all sessions, and feature requests that land in the
integrator's inbox. Every API call needs the per-launch token, and the Host
header must be local, so other web pages cannot drive the team through it.

The Merge tab is fed by independent review artifacts, never by arbitrary Git
branches. The page records approval of one unchanged reviewed branch head,
signed with the operator key, which agents verify with `hub.py approval`.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from hub import (INTEGRATOR_SESSION, ROOT, Hub, board, commit_sha, database_path, operator_key,
                 ticket_id, token)

PAGE = Path(__file__).with_name("dashboard.html")
DEFAULT_PORT = 8770  # chosen to avoid common local tool and product ports
MAIN = "main"


class Conflict(ValueError):
    """The request no longer matches the repository (HTTP 409)."""


class Dashboard:
    """State shared by request handlers; one Hub connection per operation."""

    def __init__(self, db: Path, user: str, integrator: str, secret: str) -> None:
        self.hub = Hub(db)
        self.user = token(user)
        self.integrator = token(integrator)
        self.secret = secret
        self.key = operator_key(db, create=True)
        self.merged: set[str] = set()  # approved SHAs already in main; merged stays merged

    def state(self) -> dict[str, Any]:
        branches = _branches()
        branch_heads = {branch["name"]: branch["sha"] for branch in branches}
        reviews = self.hub.reviews(latest=True)["items"]
        merge_queue = [review for review in reviews
                       if review["state"] in {"pending", "approved"}
                       and not review["blockers"]
                       and branch_heads.get(review["branch"]) == review["sha"]]
        approvals = self.hub.approvals(key=self.key)["items"]
        for approval in approvals:
            if approval["sha"] not in self.merged and _is_merged(approval["sha"]):
                self.merged.add(approval["sha"])
            approval["merged"] = approval["sha"] in self.merged
        return {
            "user": self.user,
            "integrator": self.integrator,
            "board": board(),
            "sessions": self.hub.sessions()["items"],
            "leases": self.hub.leases(limit=100)["items"],
            "features": self.hub.features()["items"],
            "branches": branches,
            "reviews": reviews,
            "merge_queue": merge_queue,
            "approvals": approvals,
            "head": _git("log", "-1", "--format=%h %s"),
        }

    def approve(self, review_id: int, sha: str, note: str) -> dict[str, Any]:
        """Approve a reviewed branch head; refuse a stale browser or moved branch."""
        review = self.hub.review(review_id)
        if ticket_id(review["ticket"]) not in {t["id"] for t in board()["tickets"]}:
            raise ValueError(f"{review['ticket']} is not in the backlog")
        if review["sha"] != sha:
            raise Conflict("The merge candidate changed; reload before approving")
        head = next((b["sha"] for b in _branches() if b["name"] == review["branch"]), None)
        if head is None:
            raise Conflict(f"{review['branch']} is not an unmerged local branch")
        if head != sha:
            raise Conflict(f"{review['branch']} moved to {head[:12]}; request a new review")
        return self.hub.approve_review(review_id, self.user, self.key, note, self.integrator)

    def feedback(self, review_id: int, sha: str, body: str) -> dict[str, Any]:
        """Send operator comments for the exact candidate currently displayed."""
        review = self.hub.review(review_id)
        if review["sha"] != sha:
            raise Conflict("The merge candidate changed; reload before sending comments")
        head = next((b["sha"] for b in _branches() if b["name"] == review["branch"]), None)
        if head != sha:
            raise Conflict("The branch moved; request a new review before sending merge feedback")
        return self.hub.review_feedback(review_id, self.user, body, self.key, self.integrator)

    def revoke(self, approval_id: int) -> dict[str, Any]:
        return self.hub.revoke_approval(approval_id, self.user, self.key, self.integrator)

    def send(self, recipient: str, body: str) -> list[dict[str, Any]]:
        recipients = ([s["session"] for s in self.hub.sessions()["items"]]
                      if recipient == "all" else [token(recipient)])
        if not recipients:
            raise ValueError("No sessions to message")
        return [self.hub.send(self.user, to, f"dash-{uuid.uuid4().hex[:12]}", body) for to in recipients]


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), *args],
                              capture_output=True, text=True, timeout=5, encoding="utf-8").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _branches() -> list[dict[str, Any]]:
    """Local branches not yet merged into main, newest commit first."""
    lines = _git("for-each-ref", f"--no-merged={MAIN}", "--sort=-committerdate",
                 "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)%09%(subject)",
                 "refs/heads").splitlines()
    branches = []
    for line in lines:
        name, sha, when, subject = (line.split("\t", 3) + ["", "", ""])[:4]
        try:
            branches.append({"name": token(name), "sha": commit_sha(sha), "time": int(when or 0),
                             "subject": subject})
        except ValueError:
            continue  # a name the hub cannot record
    return branches


def _is_merged(sha: str) -> bool:
    try:
        return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), "merge-base",
                               "--is-ancestor", sha, MAIN], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_handler(dashboard: Dashboard, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "RLGameDashboard/1"

        def log_message(self, *_: Any) -> None:  # keep the console readable
            pass

        def _reply(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._reply(403, {"error": "Host not allowed"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Team-Token", ""), dashboard.secret):
                self._reply(401, {"error": "Missing or wrong X-Team-Token"})
                return False
            return True

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/":
                if self._guard(api=False):
                    self._reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                return
            if not self._guard(api=True):
                return
            query = parse_qs(url.query)
            try:
                if url.path == "/api/state":
                    self._reply(200, dashboard.state())
                elif url.path == "/api/messages":
                    after = int(query.get("after", ["0"])[0])
                    self._reply(200, dashboard.hub.feed(after, 100))
                elif url.path == "/api/handoff":
                    ticket = query.get("ticket", [""])[0]
                    if not re.fullmatch(r"RL-\d{3,}", ticket):
                        raise ValueError("ticket must look like RL-NNN")
                    path = ROOT / "planning" / "handoffs" / f"{ticket}.md"
                    self._reply(200, {"ticket": ticket,
                                      "text": path.read_text(encoding="utf-8") if path.is_file() else ""})
                else:
                    self._reply(404, {"error": "Not found"})
            except (ValueError, OSError) as error:
                self._reply(400, {"error": str(error)})

        def do_POST(self) -> None:
            if not self._guard(api=True):
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._reply(415, {"error": "Send application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("Body must be 1-32768 bytes")
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/send":
                    self._reply(200, {"sent": dashboard.send(str(data.get("to", "")), str(data.get("body", "")))})
                elif self.path == "/api/features":
                    self._reply(200, dashboard.hub.feature_request(
                        dashboard.user, str(data.get("title", "")), str(data.get("body", "")),
                        dashboard.integrator))
                elif self.path == "/api/approve":
                    self._reply(200, dashboard.approve(int(data.get("review_id", 0)),
                                                       str(data.get("sha", "")),
                                                       str(data.get("note", ""))))
                elif self.path == "/api/review-feedback":
                    self._reply(200, dashboard.feedback(int(data.get("review_id", 0)),
                                                        str(data.get("sha", "")),
                                                        str(data.get("body", ""))))
                elif self.path == "/api/revoke":
                    self._reply(200, dashboard.revoke(int(data.get("id", 0))))
                else:
                    self._reply(404, {"error": "Not found"})
            except Conflict as error:
                self._reply(409, {"error": str(error)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._reply(400, {"error": str(error)})

    return Handler


def serve(db: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT, user: str = "roman",
          integrator: str = INTEGRATOR_SESSION, secret: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Create (not start) the server; returns it and the token to open the page with."""
    secret = secret or secrets.token_urlsafe(18)
    server = ThreadingHTTPServer((host, port), None)
    server.RequestHandlerClass = make_handler(Dashboard(db, user, integrator, secret), server.server_address[1])
    return server, secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub team dashboard (local only)")
    parser.add_argument("--db", type=Path, default=database_path())
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default="roman", help="hub sender ID for the human")
    parser.add_argument("--integrator", default=INTEGRATOR_SESSION, help="session that receives feature requests")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server, secret = serve(args.db, "127.0.0.1", args.port, args.user, args.integrator)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={secret}"
    print(f"Team dashboard: {url}\n(local only; Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
