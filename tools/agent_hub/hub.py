"""Dependency-free local coordination. SQLite state is NOT the project tracker."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from harness import MESSAGE_KINDS, STOP_SENTINEL, WAKE_SENTINEL, load_harness
from help_topics import topic_help

ROOT = Path(__file__).resolve().parents[2]
SESSION_PREFIX = "session:"  # lease resource marking which run occupies a session
HARNESS = load_harness()
INTEGRATOR_SESSION = HARNESS["integrator"]
INTEGRATOR_VACANCY_SECONDS = 300  # one team notice after the integrator seat sits free this long
VACANCY_SENDER = "hub"  # not a work session; sessions() may write one wake per vacancy
FEATURE_STATUSES = {"new", "ticketed", "declined"}
OPERATOR_KEY_FILE = "operator.key"  # beside the database; signs dashboard merge approvals


def token(value: str) -> str:
    """Validate a bounded resource or agent identifier."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}", value):
        raise ValueError("Use a 1-120 character identifier: letters, digits, _ . : / -")
    return value


PERSIST_MODES = ("oneshot", "watch")


def persist_mode(body: str) -> str:
    """Read persist: oneshot|watch from a locker. Absent persist line is oneshot."""
    found = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("persist:"):
            continue
        found.append(stripped[len("persist:"):].strip())
    if not found:
        return "oneshot"
    if len(found) != 1:
        raise ValueError("Locker may have only one persist: line")
    value = found[0]
    if value not in PERSIST_MODES:
        raise ValueError(f"persist must be oneshot or watch, not {value!r}")
    return value


def ticket_id(value: str) -> str:
    if not re.fullmatch(r"RL-\d{3,}", value or ""):
        raise ValueError("Ticket must look like RL-NNN")
    return value


def commit_sha(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", value or ""):
        raise ValueError("Pass the full 40-character commit SHA (git rev-parse <branch>)")
    return value


def operator_key(db_path: Path | str, create: bool = False) -> bytes | None:
    """The secret that signs merge approvals, stored beside the shared database.

    Only the dashboard creates approvals with it; checks read it to verify. A
    message or a direct insert without the key cannot pass an approval check.
    """
    path = Path(db_path).resolve().parent / OPERATOR_KEY_FILE
    if create and not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="ascii") as handle:
                handle.write(secrets.token_hex(32))
        except FileExistsError:
            pass  # another process created it first
    return bytes.fromhex(path.read_text(encoding="ascii").strip()) if path.is_file() else None


def _approval_signature(key: bytes, row: Any) -> str:
    fields = [row[name] for name in ("ticket", "branch", "sha", "approver", "note", "created")]
    keys = row.keys()
    version = row["signature_version"] if "signature_version" in keys else 1
    # V1 predates review/source metadata; keep it verifiable for approvals made
    # before RL-017. V2 signs the complete provenance shown in the audit UI.
    if version >= 2:
        fields.extend([row["source"], row["review_id"], row["recorded_by"],
                       row["recorded_holder"], version])
    return hmac.new(key, json.dumps(fields).encode("utf-8"), hashlib.sha256).hexdigest()


def _review_items(values: list[str] | None, label: str) -> list[str]:
    """Validate bounded review comments before JSON storage."""
    items = [value.strip() for value in (values or []) if value.strip()]
    if len(items) > 20 or any(len(value) > 1000 for value in items):
        raise ValueError(f"{label} accepts at most 20 comments of 1000 characters each")
    return items


def board() -> dict[str, Any]:
    """Return the tracked backlog in this checkout, without modifying it."""
    return json.loads((ROOT / HARNESS["backlog"]).read_text(encoding="utf-8"))


class Hub:
    """One connection per operation; BEGIN IMMEDIATE serializes claim updates."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL,
                    request_id TEXT NOT NULL, body TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'fyi',
                    created REAL NOT NULL, UNIQUE(sender, request_id)
                );
                CREATE INDEX IF NOT EXISTS inbox ON messages(recipient, id);
                CREATE TABLE IF NOT EXISTS lockers (
                    owner TEXT PRIMARY KEY, body TEXT NOT NULL,
                    cursor INTEGER NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supersessions (
                    session TEXT NOT NULL, holder TEXT NOT NULL,
                    superseded_by TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(session, holder)
                );
                CREATE TABLE IF NOT EXISTS work_sessions (
                    session TEXT PRIMARY KEY, created REAL NOT NULL
                );
                INSERT OR IGNORE INTO work_sessions(session, created)
                SELECT substr(resource, 9), expires FROM leases
                WHERE resource GLOB 'session:*';
                CREATE TABLE IF NOT EXISTS feature_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, ticket TEXT, resolved_by TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL, branch TEXT NOT NULL, sha TEXT NOT NULL,
                    approver TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL,
                    signature TEXT NOT NULL, revoked REAL,
                    source TEXT NOT NULL DEFAULT 'dashboard', review_id INTEGER,
                    recorded_by TEXT NOT NULL DEFAULT '', recorded_holder TEXT NOT NULL DEFAULT '',
                    signature_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS merge_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL, branch TEXT NOT NULL, sha TEXT NOT NULL,
                    author TEXT NOT NULL, reviewer TEXT NOT NULL, summary TEXT NOT NULL,
                    blockers TEXT NOT NULL, comments TEXT NOT NULL,
                    state TEXT NOT NULL, feedback TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, decided REAL
                );
                CREATE INDEX IF NOT EXISTS merge_reviews_ticket ON merge_reviews(ticket, id DESC);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if "kind" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'fyi'")
            approval_columns = {row[1] for row in db.execute("PRAGMA table_info(approvals)")}
            if "source" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN source TEXT NOT NULL DEFAULT 'dashboard'")
            if "review_id" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN review_id INTEGER")
            if "recorded_by" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN recorded_by TEXT NOT NULL DEFAULT ''")
            if "recorded_holder" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN recorded_holder TEXT NOT NULL DEFAULT ''")
            if "signature_version" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN signature_version INTEGER NOT NULL DEFAULT 1")
            db.execute(
                "INSERT OR IGNORE INTO work_sessions(session, created) "
                "SELECT owner, updated FROM lockers WHERE owner GLOB ?",
                (HARNESS["session_pattern"],))
            # RL-044: listing/idle facts only. Not a sweeper (RL-013).
            work_columns = {row[1] for row in db.execute("PRAGMA table_info(work_sessions)")}
            if "ended" not in work_columns:
                db.execute("ALTER TABLE work_sessions ADD COLUMN ended REAL")
            if "active_at" not in work_columns:
                db.execute("ALTER TABLE work_sessions ADD COLUMN active_at REAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _assert_session_holder(self, db: sqlite3.Connection, owner: str,
                               holder: str | None) -> None:
        """Refuse writes from an ended, expired, or superseded session holder."""
        if owner == HARNESS["operator"]:
            return
        lease = db.execute("SELECT * FROM leases WHERE resource=?",
                           (SESSION_PREFIX + owner,)).fetchone()
        known = lease or db.execute("SELECT 1 FROM work_sessions WHERE session=?", (owner,)).fetchone()
        if not known:
            return
        if holder is None:
            raise ValueError(f"Session-owned write for {owner} requires its live holder")
        token(holder)
        superseded = db.execute("SELECT superseded_by FROM supersessions WHERE session=? AND holder=?",
                                (owner, holder)).fetchone()
        if superseded:
            raise ValueError(f"Holder {holder} was superseded by {superseded['superseded_by']}")
        if not lease or lease["expires"] <= time.time() or lease["owner"] != holder:
            raise ValueError(f"Holder {holder} does not hold live session {owner}")

    def _require_live_session_holder(self, db: sqlite3.Connection, session: str,
                                     holder: str | None) -> sqlite3.Row:
        """Require a real configured session and its current, non-superseded holder."""
        token(session)
        if not fnmatch.fnmatchcase(session, HARNESS["session_pattern"]):
            raise ValueError(f"{session} is not a configured work session")
        if holder is None:
            raise ValueError(f"Session-owned write for {session} requires its live holder")
        token(holder)
        superseded = db.execute("SELECT superseded_by FROM supersessions WHERE session=? AND holder=?",
                                (session, holder)).fetchone()
        if superseded:
            raise ValueError(f"Holder {holder} was superseded by {superseded['superseded_by']}")
        lease = db.execute("SELECT * FROM leases WHERE resource=?",
                           (SESSION_PREFIX + session,)).fetchone()
        if not lease or lease["expires"] <= time.time() or lease["owner"] != holder:
            raise ValueError(f"Holder {holder} does not hold live session {session}")
        return lease

    def _touch_holder_write(self, db: sqlite3.Connection, owner: str, now: float) -> None:
        """Stamp last holder-attributed write. Session-lease renew is excluded."""
        if owner == HARNESS["operator"]:
            return
        if not fnmatch.fnmatchcase(owner, HARNESS["session_pattern"]):
            return
        db.execute("INSERT OR IGNORE INTO work_sessions(session, created) VALUES (?, ?)",
                   (owner, now))
        db.execute("UPDATE work_sessions SET active_at=? WHERE session=?", (now, owner))

    def _maybe_announce_integrator_vacancy(self, db: sqlite3.Connection, now: float,
                                           listed: list[str]) -> None:
        """One team-wide wake when the integrator seat has been free > 300s.

        Called from sessions() (dashboard poll or CLI). No sweeper thread.
        """
        resource = SESSION_PREFIX + INTEGRATOR_SESSION
        lease = db.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
        if lease and lease["expires"] > now:
            return
        free_since = lease["expires"] if lease else None
        if free_since is None:
            row = db.execute("SELECT ended FROM work_sessions WHERE session=?",
                             (INTEGRATOR_SESSION,)).fetchone()
            free_since = row["ended"] if row else None
        if free_since is None or now - free_since <= INTEGRATOR_VACANCY_SECONDS:
            return
        stamp = str(int(free_since))
        body = (f"The integrator seat ({INTEGRATOR_SESSION}) has been free for more than "
                f"{int(INTEGRATOR_VACANCY_SECONDS)} seconds. Pick up {INTEGRATOR_SESSION}.")
        recipients = list(dict.fromkeys([*listed, HARNESS["operator"]]))
        for recipient in recipients:
            request_id = f"integrator-vacancy-{stamp}-{recipient}"
            old = db.execute("SELECT 1 FROM messages WHERE sender=? AND request_id=?",
                             (VACANCY_SENDER, request_id)).fetchone()
            if old:
                continue
            try:
                self._insert_message(db, VACANCY_SENDER, recipient, request_id, body, "wake")
            except sqlite3.IntegrityError:
                pass

    def claim(self, resource: str, owner: str, seconds: int = 1800,
              holder: str | None = None) -> dict[str, Any]:
        """Claim or renew an advisory lease; conflicting live owners are refused."""
        token(resource)
        token(owner)
        if resource.startswith(SESSION_PREFIX):
            raise ValueError("Session leases only via session_start")
        if not 30 <= seconds <= 7200:
            raise ValueError("Lease seconds must be between 30 and 7200")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_session_holder(db, owner, holder)
            now = time.time()
            old = db.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
            if old and old["expires"] > now and old["owner"] != owner:
                return {"acquired": False, **dict(old)}
            db.execute("INSERT OR REPLACE INTO leases VALUES (?, ?, ?)",
                       (resource, owner, now + seconds))
            self._touch_holder_write(db, owner, now)
            return {"acquired": True, "resource": resource, "owner": owner,
                    "expires": now + seconds}

    def release(self, resource: str, owner: str, holder: str | None = None) -> dict[str, bool]:
        """Release only the named owner's lease; safe to retry."""
        token(resource)
        token(owner)
        if resource.startswith(SESSION_PREFIX):
            raise ValueError("Session leases only via session_end")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_session_holder(db, owner, holder)
            result = db.execute("DELETE FROM leases WHERE resource=? AND owner=?",
                                (resource, owner))
            released = result.rowcount == 1
            if released:
                self._touch_holder_write(db, owner, time.time())
            return {"released": released}

    def leases(self, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read a bounded page of live leases."""
        self.page(offset, limit)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM leases WHERE expires>? ORDER BY resource LIMIT ? OFFSET ?",
                              (time.time(), limit + 1, offset)).fetchall()
        return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit,
                "next_offset": offset + limit if len(rows) > limit else None}

    def send(self, sender: str, recipient: str, request_id: str, body: str,
             holder: str | None = None, kind: str = "fyi") -> dict[str, Any]:
        """Persist a message once per sender/request ID; retries must match payload."""
        for value in (sender, recipient, request_id):
            token(value)
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"kind must be one of {MESSAGE_KINDS}")
        if not body.strip() or len(body) > 8000:
            raise ValueError("Message must contain 1-8000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_session_holder(db, sender, holder)
            old = db.execute("SELECT * FROM messages WHERE sender=? AND request_id=?",
                             (sender, request_id)).fetchone()
            if old:
                if old["recipient"] != recipient or old["body"] != body or old["kind"] != kind:
                    raise ValueError("request_id reused with different content; use a new ID")
                return dict(old)
            message_id = self._insert_message(db, sender, recipient, request_id, body, kind)
            self._touch_holder_write(db, sender, time.time())
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    @staticmethod
    def _insert_message(db: sqlite3.Connection, sender: str, recipient: str, request_id: str,
                        body: str, kind: str = "fyi") -> int:
        """The single place that writes a message row (inside the caller's transaction)."""
        return db.execute(
            "INSERT INTO messages(sender,recipient,request_id,body,kind,created) VALUES (?,?,?,?,?,?)",
            (sender, recipient, request_id, body[:8000], kind, time.time())).lastrowid

    @staticmethod
    def page(cursor: int, limit: int) -> None:
        """Keep queries bounded and reject invalid pagination."""
        if cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("Cursor must be >= 0 and limit between 1 and 100")

    def inbox(self, recipient: str, after_id: int = 0, limit: int = 50) -> dict[str, Any]:
        """Read messages after a cursor, without destructive reads or acknowledgements."""
        token(recipient)
        self.page(after_id, limit)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE recipient=? AND id>? ORDER BY id LIMIT ?",
                              (recipient, after_id, limit + 1)).fetchall()
        items = [dict(r) for r in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit,
                "next_cursor": items[-1]["id"] if items else after_id}

    def feed(self, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        """Read all messages after a cursor, across recipients (the team feed)."""
        self.page(after_id, limit)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE id>? ORDER BY id LIMIT ?",
                              (after_id, limit + 1)).fetchall()
        items = [dict(r) for r in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit,
                "next_cursor": items[-1]["id"] if items else after_id}

    def feature_request(self, requester: str, title: str, body: str | None = None,
                        notify: str = INTEGRATOR_SESSION, holder: str | None = None) -> dict[str, Any]:
        """Record a feature request and notify the integrator in one transaction.

        The backlog stays single-writer: the integrator turns requests into RL
        tickets and links them with feature_update.
        """
        token(requester)
        token(notify)
        title, body = (title or "").strip(), (body or "").strip()
        if not 1 <= len(title) <= 120 or len(body) > 6000:
            raise ValueError("Feature title must be 1-120 characters and details at most 6000")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_session_holder(db, requester, holder)
            now = time.time()
            request_id = db.execute(
                "INSERT INTO feature_requests(requester,title,body,status,created,updated) "
                "VALUES (?,?,?,?,?,?)", (requester, title, body, "new", now, now)).lastrowid
            note = (f"FR-{request_id} feature request from {requester}: {title}\n\n{body}\n\n"
                    f"Triage it into an RL ticket, then: hub.py feature-update {request_id} "
                    f"--status ticketed --ticket RL-NNN --owner <session> --holder <holder>")
            self._insert_message(db, requester, notify, f"feature-{request_id}", note)
            return dict(db.execute("SELECT * FROM feature_requests WHERE id=?", (request_id,)).fetchone())

    def features(self, status: str | None = None) -> dict[str, Any]:
        """List feature requests, newest first, optionally filtered by status."""
        if status is not None and status not in FEATURE_STATUSES:
            raise ValueError(f"Status must be one of {sorted(FEATURE_STATUSES)}")
        with self.connect() as db:
            query = "SELECT * FROM feature_requests" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
            rows = db.execute(query, (status,) if status else ()).fetchall()
        return {"items": [dict(r) for r in rows]}

    def feature_update(self, request_id: int, status: str, owner: str, holder: str | None = None,
                       ticket: str | None = None) -> dict[str, Any]:
        """Mark a request ticketed (with its RL ID) or declined, and tell the requester."""
        token(owner)
        if status not in FEATURE_STATUSES:
            raise ValueError(f"Status must be one of {sorted(FEATURE_STATUSES)}")
        if status == "ticketed" and not re.fullmatch(r"RL-\d{3,}", ticket or ""):
            raise ValueError("A ticketed request needs --ticket RL-NNN")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_session_holder(db, owner, holder)
            request = db.execute("SELECT * FROM feature_requests WHERE id=?", (request_id,)).fetchone()
            if request is None:
                raise ValueError(f"No feature request FR-{request_id}")
            now = time.time()
            db.execute("UPDATE feature_requests SET status=?, ticket=?, resolved_by=?, updated=? WHERE id=?",
                       (status, ticket if status == "ticketed" else None, owner, now, request_id))
            outcome = f"is now {ticket}" if status == "ticketed" else f"was marked {status}"
            self._insert_message(db, owner, request["requester"],
                                 f"feature-{request_id}-{status}-{uuid.uuid4().hex[:8]}",
                                 f"FR-{request_id} ({request['title']}) {outcome}.")
            return dict(db.execute("SELECT * FROM feature_requests WHERE id=?", (request_id,)).fetchone())

    def review_submit(self, ticket: str, branch: str, sha: str, author: str | None, reviewer: str,
                      summary: str, blockers: list[str] | None = None,
                      comments: list[str] | None = None, holder: str | None = None,
                      notify: str = INTEGRATOR_SESSION) -> dict[str, Any]:
        """Record an independent review of one exact branch head.

        This is the only path into the dashboard merge queue. The reviewer must
        hold a live session distinct from the author session. A later review
        supersedes older reviews and revokes their still-active approvals.
        """
        ticket_id(ticket)
        commit_sha(sha)
        for value in (branch, reviewer, notify):
            token(value)
        if author is not None:
            token(author)
        summary = (summary or "").strip()
        if not summary or len(summary) > 2000:
            raise ValueError("Review summary must contain 1-2000 characters")
        blockers = _review_items(blockers, "blockers")
        comments = _review_items(comments, "comments")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_live_session_holder(db, reviewer, holder)
            now = time.time()
            claim = db.execute("SELECT * FROM leases WHERE resource=? AND expires>?",
                               (ticket, now)).fetchone()
            if claim is None:
                raise ValueError(f"{ticket} must have a live author claim before review")
            if author is not None and author != claim["owner"]:
                raise ValueError(f"Declared author {author} does not match ticket claimant {claim['owner']}")
            author = claim["owner"]
            if author == reviewer:
                raise ValueError("The ticket claimant cannot review its own work")
            author_lease = db.execute("SELECT * FROM leases WHERE resource=? AND expires>?",
                                      (SESSION_PREFIX + author, now)).fetchone()
            if author_lease is None:
                raise ValueError(f"Author session {author} is not live")
            if author_lease["owner"] == holder:
                raise ValueError("One holder cannot act as both author and reviewer")
            # A new review is authoritative for the ticket. Do not let a prior
            # approval survive a review that found blockers or covered a new SHA.
            db.execute("UPDATE approvals SET revoked=? WHERE ticket=? AND revoked IS NULL", (now, ticket))
            state = "blocked" if blockers else "pending"
            review_id = db.execute(
                "INSERT INTO merge_reviews(ticket,branch,sha,author,reviewer,summary,blockers,comments,state,created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ticket, branch, sha, author, reviewer, summary, json.dumps(blockers),
                 json.dumps(comments), state, now)).lastrowid
            details = "\n".join(f"- {item}" for item in (blockers or comments))
            text = (f"R-{review_id}: {reviewer} reviewed {ticket} on {branch} @ {sha[:12]}; "
                    f"{len(blockers)} blocking and {len(comments)} non-blocking comments.\n{summary}" +
                    (f"\n{details}" if details else ""))
            for recipient in dict.fromkeys([author, notify]):
                self._insert_message(db, reviewer, recipient, f"review-{review_id}-{recipient}",
                                     text, "review")
            return self._review_item(db.execute("SELECT * FROM merge_reviews WHERE id=?",
                                                (review_id,)).fetchone())

    @staticmethod
    def _review_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["blockers"] = json.loads(item["blockers"])
        item["comments"] = json.loads(item["comments"])
        return item

    def reviews(self, ticket: str | None = None, latest: bool = False,
                limit: int = 100) -> dict[str, Any]:
        """List review artifacts. latest=True returns only the newest per ticket."""
        if ticket is not None:
            ticket_id(ticket)
        self.page(0, limit)
        with self.connect() as db:
            where, params = (" WHERE ticket=?", [ticket]) if ticket else ("", [])
            if latest:
                query = ("SELECT r.* FROM merge_reviews r JOIN "
                         "(SELECT ticket, MAX(id) id FROM merge_reviews" + where +
                         " GROUP BY ticket) newest ON newest.id=r.id ORDER BY r.id DESC LIMIT ?")
            else:
                query = "SELECT * FROM merge_reviews" + where + " ORDER BY id DESC LIMIT ?"
            rows = db.execute(query, (*params, limit)).fetchall()
        return {"items": [self._review_item(row) for row in rows]}

    def review(self, review_id: int) -> dict[str, Any]:
        """Read one review artifact by id."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM merge_reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise ValueError(f"No merge review R-{review_id}")
        return self._review_item(row)

    def _eligible_review(self, db: sqlite3.Connection, review_id: int) -> sqlite3.Row:
        review = db.execute("SELECT * FROM merge_reviews WHERE id=?", (review_id,)).fetchone()
        if review is None:
            raise ValueError(f"No merge review R-{review_id}")
        newest = db.execute("SELECT MAX(id) FROM merge_reviews WHERE ticket=?",
                            (review["ticket"],)).fetchone()[0]
        if review["id"] != newest:
            raise ValueError("A newer review superseded this merge candidate")
        if json.loads(review["blockers"]):
            raise ValueError("A review with blocking comments cannot be approved")
        if review["state"] not in {"pending", "approved"}:
            raise ValueError(f"Review is {review['state']}, not ready for approval")
        return review

    def _insert_approval(self, db: sqlite3.Connection, ticket: str, branch: str, sha: str,
                         approver: str, key: bytes, note: str, notify: str, source: str,
                         review_id: int, recorded_by: str,
                         recorded_holder: str = "") -> dict[str, Any]:
        row = {"ticket": ticket, "branch": branch, "sha": sha, "approver": approver,
               "note": note, "created": time.time(), "source": source,
               "review_id": review_id, "recorded_by": recorded_by,
               "recorded_holder": recorded_holder, "signature_version": 2}
        signature = _approval_signature(key, row)
        approval_id = db.execute(
            "INSERT INTO approvals(ticket,branch,sha,approver,note,created,signature,source,review_id,"
            "recorded_by,recorded_holder,signature_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket, branch, sha, approver, note, row["created"], signature, source,
             review_id, recorded_by, recorded_holder, row["signature_version"])).lastrowid
        text = (f"A-{approval_id}: {approver} approved merging {branch} @ {sha[:12]} into main "
                f"for {ticket} via {source}, recorded by {recorded_by}." +
                (f"\nNote: {note}" if note else "") +
                f"\nThis message is not the approval. Right before merging, run: "
                f"hub.py approval {ticket} --sha {sha}")
        self._notify_approval(db, approval_id, recorded_by, notify, ticket, "", text)
        return self._approval_item(key, db.execute("SELECT * FROM approvals WHERE id=?",
                                                   (approval_id,)).fetchone())

    def approve_review(self, review_id: int, approver: str, key: bytes, note: str = "",
                       notify: str = INTEGRATOR_SESSION) -> dict[str, Any]:
        """Dashboard: approve the newest independently reviewed exact commit."""
        token(approver)
        token(notify)
        note = (note or "").strip()
        if len(note) > 1000:
            raise ValueError("Approval note must be at most 1000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            review = self._eligible_review(db, review_id)
            existing = db.execute(
                "SELECT * FROM approvals WHERE review_id=? AND revoked IS NULL ORDER BY id DESC LIMIT 1",
                (review_id,)).fetchone()
            if existing is not None:
                item = self._approval_item(key, existing)
                if item["status"] != "approved":
                    raise ValueError("Existing approval fails its signature check")
                return item
            approval = self._insert_approval(db, review["ticket"], review["branch"], review["sha"],
                                             approver, key, note, notify, "dashboard", review_id,
                                             approver)
            db.execute("UPDATE merge_reviews SET state='approved', decided=? WHERE id=?",
                       (time.time(), review_id))
            return approval

    def chat_approval(self, ticket: str, branch: str, sha: str, recorder: str,
                      holder: str, key: bytes, note: str = "",
                      notify: str = INTEGRATOR_SESSION) -> dict[str, Any]:
        """Integrator: record Roman's explicit approval from its direct chat."""
        ticket_id(ticket)
        commit_sha(sha)
        for value in (branch, recorder, notify):
            token(value)
        if recorder != INTEGRATOR_SESSION:
            raise ValueError(f"Only {INTEGRATOR_SESSION} may record a direct-chat approval")
        note = (note or "").strip()
        if len(note) > 1000:
            raise ValueError("Approval note must be at most 1000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_live_session_holder(db, recorder, holder)
            review = db.execute(
                "SELECT * FROM merge_reviews WHERE ticket=? ORDER BY id DESC LIMIT 1", (ticket,)).fetchone()
            if review is None or review["branch"] != branch or review["sha"] != sha:
                raise ValueError("Direct-chat approval needs the latest review of this exact branch and SHA")
            review = self._eligible_review(db, review["id"])
            existing = db.execute(
                "SELECT * FROM approvals WHERE review_id=? AND revoked IS NULL ORDER BY id DESC LIMIT 1",
                (review["id"],)).fetchone()
            if existing is not None:
                item = self._approval_item(key, existing)
                if item["status"] != "approved":
                    raise ValueError("Existing approval fails its signature check")
                return item
            approval = self._insert_approval(db, ticket, branch, sha, "roman", key, note,
                                             notify, "chat", review["id"], recorder, holder)
            db.execute("UPDATE merge_reviews SET state='approved', decided=? WHERE id=?",
                       (time.time(), review["id"]))
            return approval

    def review_feedback(self, review_id: int, sender: str, feedback: str, key: bytes,
                        notify: str = INTEGRATOR_SESSION) -> dict[str, Any]:
        """Dashboard: request changes, revoke this candidate, and notify its author."""
        token(sender)
        token(notify)
        feedback = (feedback or "").strip()
        if not feedback or len(feedback) > 4000:
            raise ValueError("Feedback must contain 1-4000 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            review = self._eligible_review(db, review_id)
            now = time.time()
            db.execute("UPDATE merge_reviews SET state='changes_requested', feedback=?, decided=? WHERE id=?",
                       (feedback, now, review_id))
            db.execute("UPDATE approvals SET revoked=? WHERE ticket=? AND sha=? AND revoked IS NULL",
                       (now, review["ticket"], review["sha"]))
            text = (f"R-{review_id}: Roman sent changes for {review['ticket']} on "
                    f"{review['branch']} @ {review['sha'][:12]}.\n{feedback}\n"
                    "The reviewed commit is not approved; update it and request a new independent review.")
            for recipient in dict.fromkeys([review["author"], notify]):
                self._insert_message(db, sender, recipient,
                                     f"review-{review_id}-feedback-{recipient}", text, "review")
            return self._review_item(db.execute("SELECT * FROM merge_reviews WHERE id=?",
                                                (review_id,)).fetchone())

    def revoke_approval(self, approval_id: int, approver: str, key: bytes,
                        notify: str = INTEGRATOR_SESSION) -> dict[str, Any]:
        """Withdraw an approval. Needs the key that signed it; repeating is harmless."""
        token(approver)
        token(notify)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["signature"], _approval_signature(key, row)):
                raise ValueError(f"No genuine approval A-{approval_id}")
            if row["revoked"] is None:
                db.execute("UPDATE approvals SET revoked=? WHERE id=?", (time.time(), approval_id))
                self._notify_approval(db, approval_id, approver, notify, row["ticket"], "-revoked",
                                      f"A-{approval_id} revoked by {approver}: do NOT merge "
                                      f"{row['branch']} @ {row['sha'][:12]} for {row['ticket']} on it.")
            return self._approval_item(key, db.execute("SELECT * FROM approvals WHERE id=?",
                                                       (approval_id,)).fetchone())

    def _notify_approval(self, db: sqlite3.Connection, approval_id: int, approver: str, notify: str,
                         ticket: str, suffix: str, text: str) -> None:
        claimant = db.execute("SELECT owner FROM leases WHERE resource=? AND expires>?",
                              (ticket, time.time())).fetchone()
        for recipient in dict.fromkeys([notify] + ([claimant["owner"]] if claimant else [])):
            self._insert_message(db, approver, recipient,
                                 f"approval-{approval_id}{suffix}-{recipient}", text)

    @staticmethod
    def _approval_item(key: bytes | None, row: sqlite3.Row) -> dict[str, Any]:
        genuine = key is not None and hmac.compare_digest(row["signature"], _approval_signature(key, row))
        item = {k: row[k] for k in row.keys() if k != "signature"}
        item["status"] = "invalid" if not genuine else "revoked" if row["revoked"] is not None else "approved"
        return item

    def approvals(self, ticket: str | None = None, key: bytes | None = None,
                  limit: int = 100) -> dict[str, Any]:
        """List approvals newest first. status: approved, revoked, or invalid (fails the key)."""
        if ticket is not None:
            ticket_id(ticket)
        self.page(0, limit)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM approvals" + (" WHERE ticket=?" if ticket else "") +
                              " ORDER BY id DESC LIMIT ?", (ticket, limit) if ticket else (limit,)).fetchall()
        return {"items": [self._approval_item(key, r) for r in rows]}

    def approval(self, ticket: str, sha: str, key: bytes | None) -> dict[str, Any]:
        """May this exact commit be merged for this ticket? Only a signed, unrevoked
        dashboard approval of this SHA counts; hub messages never do."""
        ticket_id(ticket)
        commit_sha(sha)
        items = self.approvals(ticket, key)["items"]
        match = [a for a in items if a["sha"] == sha]
        active = next((a for a in match if a["status"] == "approved"), None)
        if active:
            reason = f"A-{active['id']}: {active['approver']} approved this commit"
        elif key is None:
            reason = "No operator key, so nothing was approved from the dashboard; ask Roman"
        elif any(a["status"] == "invalid" for a in match):
            reason = "An approval record fails its signature check; do not merge and tell Roman"
        elif match:
            reason = "Roman revoked the approval of this commit"
        else:
            other = [a["sha"][:12] for a in items if a["status"] == "approved"]
            reason = (f"No approval of this commit for {ticket}"
                      + (f"; approved instead: {', '.join(other)} (a changed head needs a new approval)"
                         if other else "; ask Roman"))
        return {"ticket": ticket, "sha": sha, "approved": active is not None,
                "approval": active, "reason": reason}

    def locker(self, owner: str) -> dict[str, Any]:
        """Read an owner's saved working context; a missing locker is empty, not an error."""
        token(owner)
        with self.connect() as db:
            row = db.execute("SELECT * FROM lockers WHERE owner=?", (owner,)).fetchone()
        return dict(row) if row else {"owner": owner, "body": "", "cursor": 0, "updated": None}

    def locker_save(self, owner: str, body: str | None = None, cursor: int | None = None,
                    holder: str | None = None) -> dict[str, Any]:
        """Replace the locker body and/or inbox cursor; omitted fields keep their saved values.

        While a session is held, only its live holder may write its locker, so a
        stale or mistaken agent cannot overwrite the current holder's context.
        """
        token(owner)
        if holder is not None:
            token(holder)
        if body is None and cursor is None:
            raise ValueError("Provide a locker body, a cursor, or both")
        if body is not None and (not body.strip() or len(body) > 8000):
            raise ValueError("Locker must contain 1-8000 characters")
        if cursor is not None and cursor < 0:
            raise ValueError("Cursor must be >= 0")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if holder is not None:
                superseded = db.execute(
                    "SELECT superseded_by FROM supersessions WHERE session=? AND holder=?",
                    (owner, holder),
                ).fetchone()
                if superseded:
                    raise ValueError(f"Holder {holder} was superseded by {superseded['superseded_by']}")
            lease = db.execute("SELECT * FROM leases WHERE resource=?",
                               (SESSION_PREFIX + owner,)).fetchone()
            if lease and lease["expires"] > time.time() and lease["owner"] != holder:
                raise ValueError(f"Session {owner} is held by {lease['owner']}; "
                                 "only that holder may write its locker")
            old = db.execute("SELECT * FROM lockers WHERE owner=?", (owner,)).fetchone()
            if old is None and body is None:
                raise ValueError(f"{owner} has no locker yet; save a body before a cursor-only update")
            stored_body = body if body is not None else old["body"]
            persist_mode(stored_body)
            now = time.time()
            row = (owner, stored_body,
                   cursor if cursor is not None else (old["cursor"] if old else 0), now)
            db.execute("INSERT OR REPLACE INTO lockers VALUES (?, ?, ?, ?)", row)
            self._touch_holder_write(db, owner, now)
            return dict(zip(("owner", "body", "cursor", "updated"), row))

    def spinup(self, owner: str, limit: int = 20) -> dict[str, Any]:
        """Load saved context: locker, messages after its cursor, and this owner's leases."""
        locker = self.locker(owner)
        inbox = self.inbox(owner, locker["cursor"], limit)
        now = time.time()
        with self.connect() as db:
            rows = db.execute("SELECT * FROM leases WHERE owner=? ORDER BY resource",
                              (owner,)).fetchall()
        leases = [{**dict(r), "expired": r["expires"] <= now} for r in rows]
        return {"locker": locker, "inbox": inbox, "leases": leases}

    def session_start(self, session: str, holder: str, seconds: int = 1800,
                      limit: int = 20) -> dict[str, Any]:
        """Occupy a work session for one agent run and load its saved context.

        The session ID owns the inbox, locker, and ticket claims, so work outlives
        any one agent. The holder is unique per run; repeating the call renews.
        """
        token(session)
        token(holder)
        if not 30 <= seconds <= 7200:
            raise ValueError("Lease seconds must be between 30 and 7200")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            superseded = db.execute(
                "SELECT superseded_by FROM supersessions WHERE session=? AND holder=?",
                (session, holder),
            ).fetchone()
            if superseded:
                raise ValueError(f"Holder {holder} was superseded by {superseded['superseded_by']}")
            old = db.execute("SELECT * FROM leases WHERE resource=?",
                             (SESSION_PREFIX + session,)).fetchone()
            if old and old["expires"] > now and old["owner"] != holder:
                return {"acquired": False, "session": session, "holder": old["owner"],
                        "expires": old["expires"]}
            superseded_holder = None
            if old and old["expires"] <= now and old["owner"] != holder:
                superseded_holder = old["owner"]
                db.execute("INSERT OR REPLACE INTO supersessions VALUES (?, ?, ?, ?)",
                           (session, superseded_holder, holder, now))
            db.execute("INSERT OR REPLACE INTO leases VALUES (?, ?, ?)",
                       (SESSION_PREFIX + session, holder, now + seconds))
            db.execute("INSERT OR IGNORE INTO work_sessions(session, created) VALUES (?, ?)",
                       (session, now))
            db.execute("UPDATE work_sessions SET ended=NULL WHERE session=?", (session,))
            lease = {"expires": now + seconds}
        return {"acquired": True, "session": session, "holder": holder,
                "expires": lease["expires"], "superseded_holder": superseded_holder,
                **self.spinup(session, limit)}

    def session_end(self, session: str, holder: str, body: str | None = None,
                    cursor: int | None = None) -> dict[str, Any]:
        """Optionally save the locker, then free the session for the next agent.

        Refuses when another run holds the session, so a wrong holder cannot end it
        silently. released is false when this holder had no lease left to free.
        """
        token(session)
        token(holder)
        with self.connect() as db:
            lease = db.execute("SELECT * FROM leases WHERE resource=?",
                               (SESSION_PREFIX + session,)).fetchone()
        if lease and lease["expires"] > time.time() and lease["owner"] != holder:
            raise ValueError(f"Session {session} is held by {lease['owner']}, not {holder}")
        locker = (self.locker_save(session, body, cursor, holder)
                  if body is not None or cursor is not None else self.locker(session))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            result = db.execute("DELETE FROM leases WHERE resource=? AND owner=?",
                                (SESSION_PREFIX + session, holder))
            released = result.rowcount == 1
            db.execute("INSERT OR IGNORE INTO work_sessions(session, created) VALUES (?, ?)",
                       (session, now))
            db.execute("UPDATE work_sessions SET ended=? WHERE session=?", (now, session))
        return {"session": session, "holder": holder, "released": released, "locker": locker}

    def sessions(self) -> dict[str, Any]:
        """List open sessions: holder, locker summary, idle from holder writes, claims.

        session-end drops the seat. A sessions() call (dashboard poll or CLI) may
        send one integrator-vacancy wake; that is not a sweeper.
        """
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lockers = {r["owner"]: r for r in db.execute("SELECT * FROM lockers")}
            held = {r["resource"][len(SESSION_PREFIX):]: r for r in db.execute(
                "SELECT * FROM leases WHERE resource GLOB ? AND expires>?",  # GLOB is case-sensitive
                (SESSION_PREFIX + "*", now))}
            open_rows = {r["session"]: r for r in db.execute(
                "SELECT * FROM work_sessions WHERE ended IS NULL")}
            known = {name for name in open_rows.keys() | held.keys()
                     if fnmatch.fnmatchcase(name, HARNESS["session_pattern"]) or name in held}
            items = []
            for session in sorted(known, key=natural_key):
                locker, lease = lockers.get(session), held.get(session)
                body = locker["body"] if locker else ""
                unread = db.execute("SELECT COUNT(*) FROM messages WHERE recipient=? AND id>?",
                                    (session, locker["cursor"] if locker else 0)).fetchone()[0]
                claims = [row["resource"] for row in db.execute(
                    "SELECT resource FROM leases WHERE owner=? AND expires>? AND NOT resource GLOB ? "
                    "ORDER BY resource",
                    (session, now, SESSION_PREFIX + "*"))]
                active_at = open_rows[session]["active_at"] if session in open_rows else None
                if active_at is None and locker is not None:
                    active_at = locker["updated"]
                idle_seconds = None if active_at is None else now - active_at
                items.append({
                    "session": session, "free": lease is None,
                    "holder": lease["owner"] if lease else None,
                    "expires": lease["expires"] if lease else None,
                    "summary": next((line.strip()[:160] for line in body.splitlines() if line.strip()), ""),
                    "persist": persist_mode(body),
                    "updated": locker["updated"] if locker else None, "unread": unread,
                    "active_at": active_at,
                    "idle_seconds": idle_seconds,
                    "claims": claims,
                })
            self._maybe_announce_integrator_vacancy(db, now, [item["session"] for item in items])
        return {"items": items}

    def help(self, keyword: str | None = None) -> dict[str, Any]:
        """Return the help index or one keyword topic. Unknown keywords raise."""
        return topic_help(keyword, self)

    def holder_activity(self, session: str) -> tuple[Any, ...]:
        """Fingerprint of the writes a live holder makes: locker body or cursor,
        messages it sent, and the ticket claims it holds or released.

        Renewing the session lease is deliberately excluded, so a watcher that
        keeps polling for a dead agent cannot make that session look alive.
        """
        token(session)
        with self.connect() as db:
            locker = db.execute("SELECT updated, cursor FROM lockers WHERE owner=?",
                                (session,)).fetchone()
            sent = db.execute("SELECT MAX(id) FROM messages WHERE sender=?", (session,)).fetchone()[0]
            claims = db.execute(
                "SELECT resource, expires FROM leases WHERE owner=? AND NOT resource GLOB ? ORDER BY resource",
                (session, SESSION_PREFIX + "*")).fetchall()
        return (locker["updated"] if locker else None, locker["cursor"] if locker else None,
                sent, tuple((row["resource"], row["expires"]) for row in claims))

    def watch_poll(self, session: str, holder: str, after_id: int = 0,
                   seconds: int = 1800) -> dict[str, Any]:
        """Renew this holder and return inbox rows after after_id. Lost holders stop."""
        token(session)
        token(holder)
        self.page(after_id if after_id > 0 else 0, 50)
        try:
            started = self.session_start(session, holder, seconds)
        except ValueError as error:
            raise WatchStop(str(error)) from error
        if not started["acquired"]:
            raise WatchStop(f"Session {session} is held by {started['holder']}")
        inbox = self.inbox(session, after_id)
        return {"items": inbox["items"], "next_cursor": inbox["next_cursor"],
                "expires": started["expires"]}


class WatchStop(Exception):
    """The watch process must exit; the holder should not be re-armed."""


def run_watch(hub: Hub, session: str, holder: str, after_id: int | None = None,
              poll: float = 15, renew: int = 1800, idle_exit: float = 600) -> None:
    """Print one wake sentinel per new message id. Exits on WatchStop.

    When after_id is omitted, start after the session locker cursor so a
    documented `watch --session --holder` does not replay history.

    After idle_exit seconds with no holder-attributed write the watcher stops
    renewing and exits, so a dead agent's session reaches its lease expiry and
    the next agent can take it. A live agent re-arms; there is no way to turn
    this off, because a watcher that renews forever is the bug it prevents.
    """
    if poll <= 0 or not 30 <= renew <= 7200:
        raise ValueError("poll must be > 0 and renew between 30 and 7200 seconds")
    if idle_exit < poll:
        raise ValueError("idle-exit must be at least one poll interval")
    cursor = hub.locker(session)["cursor"] if after_id is None else after_id
    activity, active_at = hub.holder_activity(session), time.monotonic()
    while True:
        try:
            page = hub.watch_poll(session, holder, cursor, renew)
        except WatchStop as error:
            print(f"{STOP_SENTINEL} {json.dumps({'reason': str(error), 'rearm': False})}",
                  flush=True)
            raise SystemExit(2) from error
        for item in page["items"]:
            print(f"{WAKE_SENTINEL} {json.dumps({'id': item['id'], 'sender': item['sender'], 'request_id': item['request_id']})}",
                  flush=True)
            cursor = item["id"]
        current = hub.holder_activity(session)
        if current != activity:
            activity, active_at = current, time.monotonic()
        elif time.monotonic() - active_at >= idle_exit:
            reason = f"idle: no holder-attributed write for {int(idle_exit)}s; not holding {session}"
            print(f"{STOP_SENTINEL} {json.dumps({'reason': reason, 'rearm': True})}", flush=True)
            return
        time.sleep(poll)


def natural_key(value: str) -> list[Any]:
    """Sort agent-session-10 after agent-session-9."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def database_path() -> Path:
    """Use an explicit shared path when coordinating multiple worktrees."""
    configured = os.environ.get("RL_AGENT_HUB_DB")
    if configured and not Path(configured).is_absolute():
        raise ValueError("RL_AGENT_HUB_DB must be absolute so worktrees share the same state")
    return Path(configured) if configured else ROOT / ".agent-state/hub.sqlite3"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=database_path())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("board")
    commands.add_parser("leases")
    claim = commands.add_parser("claim")
    claim.add_argument("resource")
    claim.add_argument("owner")
    claim.add_argument("--seconds", type=int, default=1800)
    claim.add_argument("--holder", help="required for a session owner")
    release = commands.add_parser("release")
    release.add_argument("resource")
    release.add_argument("owner")
    release.add_argument("--holder", help="required for a session owner")
    send = commands.add_parser("send")
    for name in ("sender", "recipient", "request_id", "body"):
        send.add_argument(name)
    send.add_argument("--holder", help="required for a session sender")
    send.add_argument("--kind", default="fyi", choices=MESSAGE_KINDS)
    inbox = commands.add_parser("inbox")
    inbox.add_argument("recipient")
    inbox.add_argument("--after-id", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)
    commands.add_parser("locker").add_argument("owner")

    def locker_arguments(command: argparse.ArgumentParser) -> None:
        text = command.add_mutually_exclusive_group()
        text.add_argument("--body")
        text.add_argument("--file", help="UTF-8 text file with the locker body; '-' reads stdin")
        command.add_argument("--cursor", type=int)

    save = commands.add_parser("locker-save")
    save.add_argument("owner")
    save.add_argument("--holder", help="required while the session is held")
    locker_arguments(save)
    spinup = commands.add_parser("spinup")
    spinup.add_argument("owner")
    spinup.add_argument("--limit", type=int, default=20)
    commands.add_parser("sessions")
    help_cmd = commands.add_parser("help")
    help_cmd.add_argument("keyword", nargs="?")
    start = commands.add_parser("session-start")
    start.add_argument("session")
    start.add_argument("--holder", required=True, help="unique per run, e.g. codex-20260919-0100")
    start.add_argument("--seconds", type=int, default=1800)
    start.add_argument("--limit", type=int, default=20)
    end = commands.add_parser("session-end")
    end.add_argument("session")
    end.add_argument("--holder", required=True)
    locker_arguments(end)
    feed = commands.add_parser("feed")
    feed.add_argument("--after-id", type=int, default=0)
    feed.add_argument("--limit", type=int, default=50)
    commands.add_parser("features").add_argument("--status", choices=sorted(FEATURE_STATUSES))
    request = commands.add_parser("feature-request")
    request.add_argument("requester")
    request.add_argument("--title", required=True)
    details = request.add_mutually_exclusive_group()
    details.add_argument("--body")
    details.add_argument("--file", help="UTF-8 text file with the details; '-' reads stdin")
    request.add_argument("--notify", default=INTEGRATOR_SESSION)
    request.add_argument("--holder", help="required for a session requester")
    update = commands.add_parser("feature-update")
    update.add_argument("request_id", type=int)
    update.add_argument("--status", required=True, choices=sorted(FEATURE_STATUSES))
    update.add_argument("--ticket", help="RL-NNN when --status ticketed")
    update.add_argument("--owner", required=True)
    update.add_argument("--holder", help="required for a session owner")
    review = commands.add_parser("review-submit", help="record an independent review of one exact commit")
    review.add_argument("ticket")
    review.add_argument("--branch", required=True)
    review.add_argument("--sha", required=True, help="full reviewed commit SHA")
    review.add_argument("--author", help="author session (defaults to the live ticket claimant)")
    review.add_argument("--reviewer", required=True, help="reviewer session")
    review.add_argument("--holder", required=True, help="live reviewer holder")
    review.add_argument("--summary", required=True)
    review.add_argument("--blocker", dest="blockers", action="append", default=[])
    review.add_argument("--comment", dest="comments", action="append", default=[])
    review.add_argument("--notify", default=INTEGRATOR_SESSION)
    reviews = commands.add_parser("reviews")
    reviews.add_argument("--ticket")
    reviews.add_argument("--latest", action="store_true")
    reviews.add_argument("--limit", type=int, default=100)
    # Dashboard creates ordinary approvals. The integrator may only record a
    # direct-chat approval after Roman says yes in that chat.
    commands.add_parser("approvals").add_argument("--ticket")
    approval = commands.add_parser("approval", help="exit 0 only if this exact commit is approved")
    approval.add_argument("ticket")
    approval.add_argument("--sha", required=True, help="full commit SHA you are about to merge")
    chat_approval = commands.add_parser("chat-approval", help="integrator records Roman's direct-chat yes")
    chat_approval.add_argument("ticket")
    chat_approval.add_argument("--branch", required=True)
    chat_approval.add_argument("--sha", required=True)
    chat_approval.add_argument("--recorder", required=True, help=f"must be {INTEGRATOR_SESSION}")
    chat_approval.add_argument("--holder", required=True, help="live integrator holder")
    chat_approval.add_argument("--note", default="")
    chat_approval.add_argument("--notify", default=INTEGRATOR_SESSION)
    watch = commands.add_parser("watch")
    watch.add_argument("--session", required=True)
    watch.add_argument("--holder", required=True)
    watch.add_argument("--after-id", type=int, default=None,
                      help="inbox cursor; omitted uses the session locker cursor")
    watch.add_argument("--poll", type=float, default=15)
    watch.add_argument("--renew", type=int, default=1800)
    watch.add_argument("--idle-exit", type=float, default=600,
                       help="seconds without a holder write before the watcher stops renewing")
    args = vars(parser.parse_args())
    command, path = args.pop("command").replace("-", "_"), args.pop("db")
    try:
        source = args.pop("file", None)
        if source is not None:
            args["body"] = (sys.stdin.read() if source == "-"
                            else Path(source).read_text(encoding="utf-8"))
        if command == "watch":
            run_watch(Hub(path), args["session"], args["holder"], args["after_id"],
                      args["poll"], args["renew"], args["idle_exit"])
            return
        if command in {"approvals", "approval", "chat_approval"}:
            args["key"] = operator_key(path)
            if command == "chat_approval" and args["key"] is None:
                raise ValueError("No operator key; start the dashboard once before recording chat approval")
        result = board() if command == "board" else getattr(Hub(path), command)(**args)
        print(json.dumps(result, indent=2))
        if command in {"claim", "session_start"} and not result["acquired"]:
            raise SystemExit(2)
        if command == "approval" and not result["approved"]:
            raise SystemExit(2)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
