import io
import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import STOP_SENTINEL, WAKE_SENTINEL, load_harness
from hub import Hub, WatchStop, operator_key, run_watch


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hub.db"
        self.hub = Hub(self.path)

    def test_concurrent_claim_has_one_winner(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: Hub(self.path).claim("contracts", f"worker-{i}"), range(8)))
        self.assertEqual(sum(r["acquired"] for r in results), 1)

    def test_expiry_renewal_and_wrong_owner_release(self):
        with patch("hub.time.time", return_value=100):
            self.hub.claim("shared-editor", "a", 30)
            self.assertFalse(self.hub.release("shared-editor", "b")["released"])
        with patch("hub.time.time", return_value=120):
            self.assertTrue(self.hub.claim("shared-editor", "a", 30)["acquired"])
        with patch("hub.time.time", return_value=140):
            self.assertFalse(self.hub.claim("shared-editor", "b")["acquired"])
        with patch("hub.time.time", return_value=151):
            self.assertEqual(self.hub.leases()["items"], [])
            self.assertTrue(self.hub.claim("shared-editor", "b")["acquired"])
            self.assertFalse(self.hub.release("shared-editor", "a")["released"])

    def test_retry_deduplication_persistence_and_cursor(self):
        first = self.hub.send("a", "b", "r1", "ready")
        self.assertEqual(first, Hub(self.path).send("a", "b", "r1", "ready"))
        with self.assertRaises(ValueError):
            self.hub.send("a", "b", "r1", "different")
        self.hub.send("a", "c", "r2", "private route")
        last = self.hub.send("a", "b", "r3", "review")
        page = self.hub.inbox("b", limit=1)
        self.assertTrue(page["has_more"])
        next_page = Hub(self.path).inbox("b", page["next_cursor"])
        self.assertEqual(next_page["items"], [last])
        self.assertFalse(next_page["has_more"])
        self.assertEqual(self.hub.inbox("b", last["id"])["items"], [])

    def test_locker_roundtrip_partial_update_and_persistence(self):
        self.assertEqual(self.hub.locker("a"), {"owner": "a", "body": "", "cursor": 0, "updated": None})
        self.hub.locker_save("a", "ticket RL-005\nnext: fixtures")
        self.hub.locker_save("a", cursor=7)
        self.assertEqual(Hub(self.path).locker("a")["body"], "ticket RL-005\nnext: fixtures")
        self.hub.locker_save("a", "replaced")
        saved = Hub(self.path).locker("a")
        self.assertEqual((saved["body"], saved["cursor"]), ("replaced", 7))
        self.assertEqual(self.hub.locker("b")["body"], "")

    def test_spinup_reads_after_cursor_and_only_own_leases(self):
        seen = self.hub.send("x", "a", "r1", "already handled")
        new = self.hub.send("x", "a", "r2", "review ready")
        self.hub.send("x", "b", "r3", "not for a")
        self.hub.locker_save("a", "notes", seen["id"])
        with patch("hub.time.time", return_value=100):
            self.hub.claim("RL-005", "a", 30)
            self.hub.claim("contracts", "b", 30)
            self.hub.claim("shared-editor", "a", 60)
        with patch("hub.time.time", return_value=140):
            context = Hub(self.path).spinup("a")
        self.assertEqual(context["locker"]["body"], "notes")
        self.assertEqual(context["inbox"]["items"], [new])
        self.assertEqual([(l["resource"], l["expired"]) for l in context["leases"]],
                         [("RL-005", True), ("shared-editor", False)])

    def test_session_holders_exclude_each_other_until_expiry(self):
        self.hub.locker_save("agent-session-1", "role: integrator\nnext: merge RL-002")
        with patch("hub.time.time", return_value=100):
            first = self.hub.session_start("agent-session-1", "codex-run-1", 60)
            self.assertTrue(first["acquired"])
            self.assertEqual(first["locker"]["body"], "role: integrator\nnext: merge RL-002")
            busy = Hub(self.path).session_start("agent-session-1", "cursor-run-1")
            self.assertEqual((busy["acquired"], busy["holder"]), (False, "codex-run-1"))
        with patch("hub.time.time", return_value=130):
            self.assertTrue(self.hub.session_start("agent-session-1", "codex-run-1", 60)["acquired"])
        with patch("hub.time.time", return_value=191):
            self.assertTrue(self.hub.session_start("agent-session-1", "cursor-run-1")["acquired"])

    def test_only_live_holder_writes_a_held_locker(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-2", "old-run", 30)
            self.hub.locker_save("agent-session-2", "mine", holder="old-run")
        with patch("hub.time.time", return_value=140):
            self.hub.session_start("agent-session-2", "new-run")
            for stale in ("old-run", None):
                with self.assertRaises(ValueError):
                    self.hub.locker_save("agent-session-2", "clobber", holder=stale)
            with self.assertRaises(ValueError):
                self.hub.session_end("agent-session-2", "old-run", "clobber")
            self.assertEqual(self.hub.locker("agent-session-2")["body"], "mine")
            ended = self.hub.session_end("agent-session-2", "new-run", "handed off", 5)
        self.assertTrue(ended["released"])
        self.assertEqual((ended["locker"]["body"], ended["locker"]["cursor"]), ("handed off", 5))
        self.hub.locker_save("agent-session-2", "free sessions accept prepared context")

    def test_expired_takeover_supersedes_old_holder_for_session_owned_writes(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-7", "dropped-run", 30)
            self.hub.claim("RL-007", "agent-session-7", holder="dropped-run")
            self.hub.send("agent-session-7", "reviewer", "before-drop", "ready",
                          holder="dropped-run")
        with patch("hub.time.time", return_value=131):
            takeover = self.hub.session_start("agent-session-7", "replacement-run", 60)
            self.assertEqual(takeover["superseded_holder"], "dropped-run")
            self.assertTrue(self.hub.claim("RL-007", "agent-session-7",
                                           holder="replacement-run")["acquired"])
            for operation in (
                lambda: self.hub.claim("RL-007", "agent-session-7", holder="dropped-run"),
                lambda: self.hub.release("RL-007", "agent-session-7", holder="dropped-run"),
                lambda: self.hub.send("agent-session-7", "reviewer", "after-drop", "late",
                                      holder="dropped-run"),
            ):
                with self.assertRaisesRegex(ValueError, "superseded"):
                    operation()
            self.hub.session_end("agent-session-7", "replacement-run")
            with self.assertRaisesRegex(ValueError, "superseded"):
                self.hub.claim("RL-007", "agent-session-7", holder="dropped-run")
            with self.assertRaisesRegex(ValueError, "superseded"):
                self.hub.session_start("agent-session-7", "dropped-run")
            with self.assertRaisesRegex(ValueError, "Session leases only"):
                self.hub.claim("session:agent-session-7", "dropped-run")
            with self.assertRaisesRegex(ValueError, "Session leases only"):
                self.hub.release("session:agent-session-7", "dropped-run")
            with self.assertRaisesRegex(ValueError, "superseded"):
                self.hub.locker_save("agent-session-7", "clobber", holder="dropped-run")
            with self.assertRaisesRegex(ValueError, "superseded"):
                self.hub.session_end("agent-session-7", "dropped-run", "clobber")
            self.hub.locker_save("agent-session-7", "prepared context after successor ended")
            self.assertEqual(self.hub.locker("agent-session-7")["body"],
                             "prepared context after successor ended")

    def test_session_owned_writes_require_a_live_matching_holder(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-8", "live-run", 30)
            with self.assertRaisesRegex(ValueError, "requires its live holder"):
                self.hub.claim("RL-008", "agent-session-8")
        with patch("hub.time.time", return_value=131):
            with self.assertRaisesRegex(ValueError, "does not hold live session"):
                self.hub.claim("RL-008", "agent-session-8", holder="live-run")

    def test_existing_session_leases_are_migrated_to_holder_guards(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("legacy-session", "legacy-run", 60)
            migrated = Hub(self.path)
            with self.assertRaisesRegex(ValueError, "requires its live holder"):
                migrated.claim("RL-legacy", "legacy-session")
            self.assertTrue(migrated.claim("RL-legacy", "legacy-session",
                                           holder="legacy-run")["acquired"])

    def test_wrong_holder_cannot_end_a_held_session(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-4", "real-run", 60)
            with self.assertRaises(ValueError):
                self.hub.session_end("agent-session-4", "other-run")
            self.assertFalse(self.hub.session_start("agent-session-4", "other-run")["acquired"])
        with patch("hub.time.time", return_value=200):
            self.assertFalse(self.hub.session_end("agent-session-4", "other-run")["released"])

    def test_session_listing_ignores_other_cased_prefixes(self):
        self.hub.claim("SESSION:shouting", "someone")
        self.hub.session_start("quiet", "someone")
        self.assertEqual([i["session"] for i in self.hub.sessions()["items"]], ["quiet"])

    def test_sessions_list_occupancy_summary_and_unread(self):
        self.hub.locker_save("agent-session-10", "\n  role: reviewer  \nmore", 1)
        self.hub.locker_save("agent-session-9", "role: implementer")
        self.hub.send("x", "agent-session-10", "r1", "seen")
        self.hub.send("x", "agent-session-10", "r2", "unread")
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-3", "claude-run", 60)
        with patch("hub.time.time", return_value=120):
            items = self.hub.sessions()["items"]
        self.assertEqual([i["session"] for i in items],
                         ["agent-session-3", "agent-session-9", "agent-session-10"])
        self.assertEqual([(i["free"], i["holder"]) for i in items],
                         [(False, "claude-run"), (True, None), (True, None)])
        self.assertEqual([i["summary"] for i in items], ["", "role: implementer", "role: reviewer"])
        self.assertEqual([i["persist"] for i in items], ["oneshot", "oneshot", "oneshot"])
        self.assertEqual([i["unread"] for i in items], [0, 0, 1])
        self.assertEqual([i["claims"] for i in items], [[], [], []])
        for item in items:
            self.assertIn("active_at", item)
            self.assertIn("idle_seconds", item)

    def test_sessions_persist_from_locker_and_rejects_bad_values(self):
        self.hub.locker_save("agent-session-1", "role: integrator\npersist: watch\nnext: wait")
        self.hub.locker_save("agent-session-2", "role: general\npersist: oneshot")
        self.assertEqual(
            [i["persist"] for i in self.hub.sessions()["items"]],
            ["watch", "oneshot"],
        )
        with self.assertRaisesRegex(ValueError, "persist must be oneshot or watch"):
            self.hub.locker_save("agent-session-1", "role: integrator\npersist: always")
        with self.assertRaisesRegex(ValueError, "only one persist"):
            self.hub.locker_save(
                "agent-session-1",
                "role: integrator\npersist: watch\npersist: oneshot",
            )
        self.assertEqual(self.hub.sessions()["items"][0]["persist"], "watch")

    def test_locker_registers_session_for_holder_guards(self):
        self.hub.locker_save("agent-session-9", "role: implementer")
        migrated = Hub(self.path)
        with self.assertRaisesRegex(ValueError, "requires its live holder"):
            migrated.claim("RL-009", "agent-session-9")

    def test_invalid_locker_requests(self):
        with self.assertRaises(ValueError):
            self.hub.locker_save("a")
        with self.assertRaises(ValueError):
            self.hub.locker_save("a", " ")
        with self.assertRaises(ValueError):
            self.hub.locker_save("a", "x" * 8001)
        with self.assertRaises(ValueError):
            self.hub.locker_save("a", cursor=-1)
        with self.assertRaises(ValueError):
            self.hub.locker_save("no-locker-yet", cursor=3)
        self.assertEqual(self.hub.sessions()["items"], [])
        with self.assertRaises(ValueError):
            self.hub.locker("bad owner")
        with self.assertRaises(ValueError):
            self.hub.spinup("a", limit=0)

    def test_feature_request_reaches_integrator_and_links_to_a_ticket(self):
        request = self.hub.feature_request("roman", "  Team dashboard ", "Cards and chat")
        self.assertEqual((request["title"], request["status"], request["ticket"]), ("Team dashboard", "new", None))
        note = self.hub.inbox("agent-session-1")["items"][-1]
        self.assertEqual((note["sender"], note["request_id"]), ("roman", f"feature-{request['id']}"))
        self.assertIn("Cards and chat", note["body"])
        self.assertEqual([f["id"] for f in self.hub.features("new")["items"]], [request["id"]])

        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-1", "cursor-run", 60)
            with self.assertRaises(ValueError):  # the integrator session must present its holder
                self.hub.feature_update(request["id"], "ticketed", "agent-session-1", ticket="RL-011")
            with self.assertRaises(ValueError):
                self.hub.feature_update(request["id"], "ticketed", "agent-session-1", "cursor-run", "not-a-ticket")
            linked = self.hub.feature_update(request["id"], "ticketed", "agent-session-1", "cursor-run", "RL-011")
        self.assertEqual((linked["status"], linked["ticket"], linked["resolved_by"]),
                         ("ticketed", "RL-011", "agent-session-1"))
        reply = self.hub.inbox("roman")["items"][-1]
        self.assertIn("RL-011", reply["body"])
        self.assertEqual(self.hub.features("new")["items"], [])

    def test_invalid_feature_requests_and_team_feed(self):
        for title, body in (("", "x"), ("x" * 121, ""), ("ok", "y" * 6001)):
            with self.assertRaises(ValueError):
                self.hub.feature_request("roman", title, body)
        with self.assertRaises(ValueError):
            self.hub.features("unknown")
        with self.assertRaises(ValueError):
            self.hub.feature_update(999, "declined", "roman")
        self.hub.send("a", "b", "r1", "one")
        self.hub.send("c", "d", "r2", "two")
        page = self.hub.feed(0, 1)
        self.assertTrue(page["has_more"])
        self.assertEqual([m["body"] for m in self.hub.feed(page["next_cursor"])["items"]], ["two"])

    def test_only_a_signed_approval_of_the_exact_commit_counts(self):
        sha, moved = "a" * 40, "b" * 40
        self.assertIsNone(operator_key(self.path))
        self.assertFalse(self.hub.approval("RL-011", sha, None)["approved"])
        key = operator_key(self.path, create=True)
        self.assertEqual(operator_key(self.path, create=True), key)  # created once, then reused
        self.hub.send("roman", "agent-session-1", "fake", "approved, merge RL-011")
        self.assertFalse(self.hub.approval("RL-011", sha, key)["approved"])  # a message is not approval
        self.hub.session_start("agent-session-2", "author-run")
        self.hub.session_start("agent-session-3", "reviewer-run")
        self.hub.claim("RL-011", "agent-session-2", holder="author-run")
        review = self.hub.review_submit("RL-011", "feature/team-dashboard", sha, None,
                                        "agent-session-3", "ready", holder="reviewer-run")
        approval = self.hub.approve_review(review["id"], "roman", key, " ship it ")
        self.assertEqual((approval["status"], approval["note"]), ("approved", "ship it"))
        self.assertTrue(self.hub.approval("RL-011", sha, key)["approved"])
        other = self.hub.approval("RL-011", moved, key)
        self.assertFalse(other["approved"])
        self.assertIn(sha[:12], other["reason"])
        self.assertFalse(self.hub.approval("RL-012", sha, key)["approved"])
        self.assertFalse(self.hub.approval("RL-011", sha, b"other key")["approved"])
        told = {m["recipient"]: m["body"] for m in self.hub.feed()["items"] if m["request_id"].startswith("approval-")}
        self.assertEqual(sorted(told), ["agent-session-1", "agent-session-2"])  # integrator and claimant
        self.assertIn(f"hub.py approval RL-011 --sha {sha}", told["agent-session-1"])

        with self.assertRaises(ValueError):  # revoking proves the key too
            self.hub.revoke_approval(approval["id"], "roman", b"other key")
        self.assertEqual(self.hub.revoke_approval(approval["id"], "roman", key)["status"], "revoked")
        self.assertEqual(self.hub.revoke_approval(approval["id"], "roman", key)["status"], "revoked")
        check = self.hub.approval("RL-011", sha, key)
        self.assertFalse(check["approved"])
        self.assertIn("revoked", check["reason"])

    def test_independent_review_gates_dashboard_and_chat_approvals(self):
        sha, moved = "6" * 40, "7" * 40
        key = operator_key(self.path, create=True)
        self.hub.session_start("agent-session-1", "author-run")
        self.hub.session_start("agent-session-2", "reviewer-run")
        self.hub.claim("RL-017", "agent-session-1", holder="author-run")
        for fake in ("bob", "roman"):
            with self.assertRaises(ValueError):
                self.hub.review_submit("RL-017", "feature/x", sha, None, fake, "fake review",
                                       holder="anything")
        with self.assertRaises(ValueError):
            self.hub.review_submit("RL-017", "feature/x", sha, "agent-session-9",
                                   "agent-session-2", "wrong author", holder="reviewer-run")

        self.hub.session_start("agent-session-3", "same-holder")
        self.hub.session_start("agent-session-4", "same-holder")
        self.hub.claim("RL-019", "agent-session-3", holder="same-holder")
        with self.assertRaises(ValueError):
            self.hub.review_submit("RL-019", "feature/y", sha, None, "agent-session-4",
                                   "same human", holder="same-holder")
        self.hub.claim("RL-018", "agent-session-2", holder="reviewer-run")
        with self.assertRaises(ValueError):
            self.hub.review_submit("RL-018", "feature/z", sha, None, "agent-session-2",
                                   "self review", holder="reviewer-run")

        blocked = self.hub.review_submit("RL-017", "feature/x", sha, None,
                                         "agent-session-2", "needs work", ["blocking"],
                                         holder="reviewer-run")
        self.assertEqual(blocked["state"], "blocked")
        with self.assertRaises(ValueError):
            self.hub.approve_review(blocked["id"], "roman", key)

        review = self.hub.review_submit("RL-017", "feature/x", sha, None,
                                        "agent-session-2", "ready", comments=["small nit"],
                                        holder="reviewer-run")
        self.assertEqual((review["state"], review["comments"]), ("pending", ["small nit"]))
        with self.assertRaises(ValueError):
            self.hub.chat_approval("RL-017", "feature/x", moved, "agent-session-1",
                                   "author-run", key)
        chat = self.hub.chat_approval("RL-017", "feature/x", sha, "agent-session-1",
                                      "author-run", key, "Roman said yes in direct chat")
        self.assertEqual((chat["source"], chat["review_id"], chat["recorded_by"],
                          chat["recorded_holder"]),
                         ("chat", review["id"], "agent-session-1", "author-run"))
        notice = [m for m in self.hub.feed()["items"] if m["request_id"].startswith("approval-")][-1]
        self.assertEqual(notice["sender"], "agent-session-1")
        self.assertTrue(self.hub.approval("RL-017", sha, key)["approved"])

        replacement = self.hub.review_submit("RL-017", "feature/x", moved, None,
                                              "agent-session-2", "new head ready",
                                              holder="reviewer-run")
        self.assertFalse(self.hub.approval("RL-017", sha, key)["approved"])
        with self.assertRaises(ValueError):
            self.hub.approve_review(review["id"], "roman", key)
        dashboard = self.hub.approve_review(replacement["id"], "roman", key)
        self.assertEqual((dashboard["source"], dashboard["sha"]), ("dashboard", moved))
        self.assertEqual(self.hub.approve_review(replacement["id"], "roman", key)["id"],
                         dashboard["id"])
        with self.hub.connect() as db:
            db.execute("UPDATE approvals SET recorded_by='forged' WHERE id=?", (dashboard["id"],))
        self.assertFalse(self.hub.approval("RL-017", moved, key)["approved"])

    def test_review_feedback_requests_changes_and_revokes_approval(self):
        sha = "8" * 40
        key = operator_key(self.path, create=True)
        self.hub.session_start("agent-session-3", "author-run")
        self.hub.session_start("agent-session-2", "reviewer-run")
        self.hub.claim("RL-017", "agent-session-3", holder="author-run")
        review = self.hub.review_submit("RL-017", "feature/x", sha, None,
                                        "agent-session-2", "ready", comments=["optional cleanup"],
                                        holder="reviewer-run")
        self.hub.approve_review(review["id"], "roman", key)
        changed = self.hub.review_feedback(review["id"], "roman", "Please do the cleanup.", key)
        self.assertEqual((changed["state"], changed["feedback"]),
                         ("changes_requested", "Please do the cleanup."))
        self.assertFalse(self.hub.approval("RL-017", sha, key)["approved"])
        author_mail = self.hub.inbox("agent-session-3")["items"][-1]
        self.assertEqual(author_mail["kind"], "review")
        self.assertIn("Please do the cleanup", author_mail["body"])

    def test_tampered_or_forged_approvals_fail_the_check(self):
        key, sha = operator_key(self.path, create=True), "c" * 40
        self.hub.session_start("agent-session-2", "author-run")
        self.hub.session_start("agent-session-3", "reviewer-run")
        self.hub.claim("RL-011", "agent-session-2", holder="author-run")
        review = self.hub.review_submit("RL-011", "feature/x", "d" * 40, None,
                                        "agent-session-3", "ready", holder="reviewer-run")
        approval = self.hub.approve_review(review["id"], "roman", key)
        with self.hub.connect() as db:
            db.execute("UPDATE approvals SET sha=? WHERE id=?", (sha, approval["id"]))
            db.execute("INSERT INTO approvals(ticket,branch,sha,approver,note,created,signature) "
                       "VALUES (?,?,?,?,?,?,?)", ("RL-011", "feature/x", sha, "roman", "", 1.0, "0" * 64))
        check = self.hub.approval("RL-011", sha, key)
        self.assertFalse(check["approved"])
        self.assertIn("signature", check["reason"])
        self.assertEqual({a["status"] for a in self.hub.approvals("RL-011", key)["items"]}, {"invalid"})

    def test_legacy_v1_approval_survives_provenance_column_migration(self):
        legacy = Path(self.temp.name) / "legacy.db"
        key, sha, created = b"legacy-key", "d" * 40, 123.0
        fields = ["RL-011", "feature/x", sha, "roman", "", created]
        signature = hmac.new(key, json.dumps(fields).encode("utf-8"), hashlib.sha256).hexdigest()
        db = sqlite3.connect(legacy)
        try:
            db.execute("CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket TEXT NOT NULL, "
                       "branch TEXT NOT NULL, sha TEXT NOT NULL, approver TEXT NOT NULL, note TEXT NOT NULL, "
                       "created REAL NOT NULL, signature TEXT NOT NULL, revoked REAL)")
            db.execute("INSERT INTO approvals(ticket,branch,sha,approver,note,created,signature) "
                       "VALUES (?,?,?,?,?,?,?)", (*fields, signature))
            db.commit()
        finally:
            db.close()
        migrated = Hub(legacy).approvals("RL-011", key)["items"][0]
        self.assertEqual((migrated["status"], migrated["signature_version"]), ("approved", 1))

    def test_invalid_approvals(self):
        key = operator_key(self.path, create=True)
        for ticket, sha in (("RL-1", "a" * 40), ("RL-011", "a" * 39), ("RL-011", "A" * 40)):
            with self.assertRaises(ValueError):
                self.hub.review_submit(ticket, "feature/x", sha, None, "agent-session-2", "review")
        with self.assertRaises(ValueError):
            self.hub.approve_review(999, "roman", key)
        with self.assertRaises(ValueError):
            self.hub.chat_approval("RL-011", "feature/x", "a" * 40,
                                   "agent-session-1", "not-live", key)
        with self.assertRaises(ValueError):
            self.hub.approval("RL-011", "abc1234", key)
        with self.assertRaises(ValueError):
            self.hub.revoke_approval(99, "roman", key)

    def test_cli_approval_exits_zero_only_when_approved(self):
        sha = "e" * 40
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "hub.py"), "--db", str(self.path),
                   "approval", "RL-011", "--sha", sha]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)
        self.hub.session_start("agent-session-2", "author-run")
        self.hub.session_start("agent-session-3", "reviewer-run")
        self.hub.claim("RL-011", "agent-session-2", holder="author-run")
        review = self.hub.review_submit("RL-011", "feature/x", sha, None,
                                        "agent-session-3", "ready", holder="reviewer-run")
        self.hub.approve_review(review["id"], "roman", operator_key(self.path, create=True))
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_cli_records_review_then_integrator_chat_approval(self):
        sha = "9" * 40
        operator_key(self.path, create=True)
        self.hub.session_start("agent-session-1", "integrator-run")
        self.hub.session_start("agent-session-2", "reviewer-run")
        self.hub.session_start("agent-session-3", "author-run")
        self.hub.claim("RL-017", "agent-session-3", holder="author-run")
        base = [sys.executable, str(Path(__file__).resolve().parents[1] / "hub.py"),
                "--db", str(self.path)]
        review = subprocess.run(base + ["review-submit", "RL-017", "--branch", "feature/x",
                                        "--sha", sha, "--author", "agent-session-3",
                                        "--reviewer", "agent-session-2", "--holder", "reviewer-run",
                                        "--summary", "ready", "--comment", "nit"],
                                capture_output=True, text=True)
        self.assertEqual(review.returncode, 0, review.stderr)
        approval = subprocess.run(base + ["chat-approval", "RL-017", "--branch", "feature/x",
                                          "--sha", sha, "--recorder", "agent-session-1",
                                          "--holder", "integrator-run", "--note", "direct yes"],
                                  capture_output=True, text=True)
        self.assertEqual(approval.returncode, 0, approval.stderr)
        item = json.loads(approval.stdout)
        self.assertEqual((item["source"], item["sha"]), ("chat", sha))

    def test_invalid_requests(self):
        with self.assertRaises(ValueError):
            self.hub.claim("contracts", "", 30)
        with self.assertRaises(ValueError):
            self.hub.claim("contracts", "a", 0)
        with self.assertRaises(ValueError):
            self.hub.inbox("a", limit=101)
        with self.assertRaises(ValueError):
            self.hub.send("a", "b", "r1", " ")

    def test_help_index_lists_required_topics(self):
        index = self.hub.help()
        names = [t["keyword"] for t in index["topics"]]
        self.assertEqual(names, ["sessions", "claims", "inbox", "mcp", "startup", "watch"])
        self.assertEqual(index["overlay"], ["skeptic"])
        expected_path = str(self.path.resolve())
        self.assertEqual(index["db_path"], expected_path)
        self.assertIn(expected_path, index["db_command"])
        for name in names + index["overlay"]:
            topic = self.hub.help(name)
            self.assertEqual(topic["keyword"], name)
            self.assertTrue(topic["commands"])
            self.assertIn("RL_AGENT_HUB_DB", topic["db"])
            self.assertEqual(topic["db_path"], expected_path)
            self.assertIn(expected_path, topic["db_command"])

    def test_help_names_persist_modes_and_forbids_second_loop(self):
        sessions = json.dumps(self.hub.help("sessions"))
        watch = json.dumps(self.hub.help("watch"))
        for blob in (sessions, watch):
            self.assertIn("persist: oneshot", blob)
            self.assertIn("persist: watch", blob)
            self.assertIn("second poller", blob)
            self.assertIn("session-start heartbeat", blob)

    def test_help_unknown_keyword_raises(self):
        with self.assertRaisesRegex(ValueError, "Unknown help topic"):
            self.hub.help("telegram")

    def test_help_startup_on_empty_and_occupied_db(self):
        empty = self.hub.help("startup")
        self.assertTrue(empty["fresh_checkout"])
        self.assertIn("product services", empty["do_not_start"])
        self.assertIn("GPU", empty["do_not_start"])
        self.hub.session_start("agent-session-1", "cursor-run", 60)
        self.assertFalse(self.hub.help("startup")["fresh_checkout"])

    def test_agents_md_keeps_load_bearing_help_stanza(self):
        text = Path(__file__).resolve().parents[3].joinpath("AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Load-bearing:", text)
        self.assertIn("hub.py help", text)
        self.assertIn("hub.py watch", text)
        self.assertLess(len(text), 4000)
        self.assertNotIn("Unity", text)
        self.assertNotIn("0.02", text)
        self.assertNotIn("Synty", text)

    def test_send_kind_and_operator_without_holder(self):
        row = self.hub.send("roman", "agent-session-1", "n1", "hello", kind="wake")
        self.assertEqual(row["kind"], "wake")
        self.assertEqual(self.hub.send("roman", "agent-session-1", "n2", "fyi body")["kind"], "fyi")
        self.assertEqual(self.hub.inbox("agent-session-1")["items"][0]["kind"], "wake")
        with self.assertRaises(ValueError):
            self.hub.send("roman", "agent-session-1", "n3", "x", kind="nudge")
        self.hub.session_start("agent-session-2", "h1", 60)
        with self.assertRaises(ValueError):
            self.hub.send("agent-session-2", "agent-session-1", "n4", "needs holder")
        self.hub.send("agent-session-2", "agent-session-1", "n4", "needs holder", "h1", "review")

    def test_harness_config_errors_and_required_docs(self):
        docs = load_harness()["required_docs"]
        self.assertTrue(docs)
        repo_root = Path(__file__).resolve().parents[3]
        if (repo_root / "specs" / "schema.py").is_file():
            self.assertIn("specs/game-rules.md", docs)
        else:
            self.assertNotIn("specs/game-rules.md", docs)
        missing = Path(self.temp.name) / "nope.json"
        with self.assertRaises(ValueError):
            load_harness(missing)
        bad = Path(self.temp.name) / "bad.json"
        bad.write_text(json.dumps({
            "backlog": "x", "session_pattern": "", "operator": "roman",
            "integrator": "agent-session-1", "reserved_resources": [], "required_docs": [],
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_harness(bad)
        kernel = Path(self.temp.name) / "kernel.json"
        kernel.write_text(json.dumps({
            "backlog": "planning/backlog.json", "session_pattern": "sdlc-*",
            "operator": "roman", "integrator": "agent-session-1",
            "reserved_resources": [], "required_docs": ["AGENTS.md"],
        }), encoding="utf-8")
        self.assertNotIn("specs/game-rules.md", load_harness(kernel)["required_docs"])

    def test_watch_poll_new_mail_idle_and_superseded_holder(self):
        self.hub.session_start("agent-session-1", "holder-a", 60)
        self.assertEqual(self.hub.watch_poll("agent-session-1", "holder-a", 0)["items"], [])
        self.hub.send("roman", "agent-session-1", "w1", "wake up", kind="wake")
        page = self.hub.watch_poll("agent-session-1", "holder-a", 0)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["kind"], "wake")
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-2", "old", 60)
        with patch("hub.time.time", return_value=200):
            self.hub.session_start("agent-session-2", "new", 60)
        with self.assertRaises(WatchStop):
            self.hub.watch_poll("agent-session-2", "old", 0)

    def test_run_watch_prints_one_sentinel_per_id(self):
        self.hub.session_start("agent-session-1", "h", 60)
        self.hub.send("roman", "agent-session-1", "a", "one")
        self.hub.send("roman", "agent-session-1", "b", "two")

        def stop(_delay: float) -> None:
            raise KeyboardInterrupt

        with patch("hub.time.sleep", side_effect=stop), patch("sys.stdout", new=io.StringIO()) as out:
            try:
                run_watch(self.hub, "agent-session-1", "h", 0, poll=1, renew=60)
            except KeyboardInterrupt:
                pass
        lines = [line for line in out.getvalue().splitlines() if line.startswith(WAKE_SENTINEL)]
        self.assertEqual(len(lines), 2)

    def test_run_watch_without_after_id_starts_at_locker_cursor(self):
        self.hub.session_start("agent-session-1", "h", 60)
        self.hub.send("roman", "agent-session-1", "old-1", "already read")
        old = self.hub.send("roman", "agent-session-1", "old-2", "also read")
        self.hub.locker_save("agent-session-1", "role: integrator", old["id"], "h")
        self.hub.send("roman", "agent-session-1", "new-1", "fresh")

        def stop(_delay: float) -> None:
            raise KeyboardInterrupt

        with patch("hub.time.sleep", side_effect=stop), patch("sys.stdout", new=io.StringIO()) as out:
            try:
                run_watch(self.hub, "agent-session-1", "h", poll=1, renew=60)
            except KeyboardInterrupt:
                pass
        lines = [line for line in out.getvalue().splitlines() if line.startswith(WAKE_SENTINEL)]
        self.assertEqual(len(lines), 1)
        self.assertIn("new-1", lines[0])

    def watch_until_idle(self, idle_exit=30, poll=1, ticks=60):
        """Run the watcher with a fake monotonic clock that advances one poll per sleep."""
        clock = [0.0]

        def tick(delay: float) -> None:
            clock[0] += delay
            if clock[0] > ticks:
                raise KeyboardInterrupt

        with patch("hub.time.monotonic", side_effect=lambda: clock[0]),                 patch("hub.time.sleep", side_effect=tick), patch("sys.stdout", new=io.StringIO()) as out:
            try:
                run_watch(self.hub, "agent-session-1", "h", 0, poll=poll, renew=60, idle_exit=idle_exit)
                timed_out = False
            except KeyboardInterrupt:
                timed_out = True
        stops = [line for line in out.getvalue().splitlines() if line.startswith(STOP_SENTINEL)]
        return stops, timed_out, clock[0]

    def test_watch_exits_when_the_holder_stops_writing(self):
        self.hub.session_start("agent-session-1", "h", 60)
        stops, timed_out, elapsed = self.watch_until_idle()
        self.assertFalse(timed_out)
        self.assertEqual(len(stops), 1)
        self.assertIn("idle", stops[0])
        self.assertTrue(json.loads(stops[0][len(STOP_SENTINEL) + 1:])["rearm"])
        self.assertGreaterEqual(elapsed, 30)

    def test_lease_renewal_and_incoming_mail_do_not_look_like_a_live_holder(self):
        # The watcher renews the session lease every poll, and mail keeps arriving,
        # but neither is a write by this holder, so the idle window still expires.
        self.hub.session_start("agent-session-1", "h", 60)
        before, clock, polls = self.hub.holder_activity("agent-session-1"), [0.0], []

        def tick(delay: float) -> None:
            clock[0] += delay
            polls.append(clock[0])
            if len(polls) in (3, 12):  # mail arrives mid-watch; the holder never answers
                self.hub.send("roman", "agent-session-1", f"wake-{len(polls)}", "are you there")
            if clock[0] > 60:
                raise KeyboardInterrupt

        with patch("hub.time.monotonic", side_effect=lambda: clock[0]),                 patch("hub.time.sleep", side_effect=tick), patch("sys.stdout", new=io.StringIO()) as out:
            run_watch(self.hub, "agent-session-1", "h", 0, poll=1, renew=60, idle_exit=30)
        printed = out.getvalue().splitlines()
        self.assertEqual(len([line for line in printed if line.startswith(WAKE_SENTINEL)]), 2)
        self.assertEqual(len([line for line in printed if line.startswith(STOP_SENTINEL)]), 1)
        self.assertEqual(self.hub.holder_activity("agent-session-1"), before)

    def test_a_writing_holder_is_never_exited_for_idle(self):
        self.hub.session_start("agent-session-1", "h", 60)
        clock, writes = [0.0], []

        def tick(delay: float) -> None:
            clock[0] += delay
            writes.append(clock[0])
            if len(writes) % 5 == 0:  # a holder write every five polls, inside the window
                self.hub.locker_save("agent-session-1", f"working at {clock[0]}", holder="h")
            if clock[0] > 120:
                raise KeyboardInterrupt

        with patch("hub.time.monotonic", side_effect=lambda: clock[0]),                 patch("hub.time.sleep", side_effect=tick), patch("sys.stdout", new=io.StringIO()) as out:
            with self.assertRaises(KeyboardInterrupt):  # only the test clock stops it
                run_watch(self.hub, "agent-session-1", "h", 0, poll=1, renew=60, idle_exit=30)
        self.assertEqual([line for line in out.getvalue().splitlines() if line.startswith(STOP_SENTINEL)], [])

    def test_claim_and_release_count_as_holder_activity(self):
        self.hub.session_start("agent-session-1", "h", 60)
        start = self.hub.holder_activity("agent-session-1")
        self.hub.claim("RL-013", "agent-session-1", holder="h")
        claimed = self.hub.holder_activity("agent-session-1")
        self.assertNotEqual(claimed, start)
        self.hub.release("RL-013", "agent-session-1", holder="h")
        self.assertNotEqual(self.hub.holder_activity("agent-session-1"), claimed)
        released = self.hub.holder_activity("agent-session-1")
        self.hub.session_start("agent-session-1", "h", 90)  # renewing the session lease alone
        self.assertEqual(self.hub.holder_activity("agent-session-1"), released)

    def test_superseded_holder_still_exits_nonzero(self):
        self.hub.session_start("agent-session-1", "first", 60)
        with patch("hub.time.time", return_value=time.time() + 120):
            self.hub.session_start("agent-session-1", "second", 60)  # takeover supersedes "first"
        with patch("sys.stdout", new=io.StringIO()) as out:
            with self.assertRaises(SystemExit) as exit_code:
                run_watch(self.hub, "agent-session-1", "first", 0, poll=1, renew=60, idle_exit=30)
        self.assertEqual(exit_code.exception.code, 2)
        stop = [line for line in out.getvalue().splitlines() if line.startswith(STOP_SENTINEL)][0]
        self.assertFalse(json.loads(stop[len(STOP_SENTINEL) + 1:])["rearm"])

    def test_idle_exit_shorter_than_one_poll_is_refused(self):
        with self.assertRaises(ValueError):
            run_watch(self.hub, "agent-session-1", "h", 0, poll=15, renew=60, idle_exit=5)

    def test_session_pattern_match_is_case_sensitive(self):
        self.hub.locker_save("AGENT-SESSION-9", "not a session id")
        self.assertEqual(self.hub.sessions()["items"], [])
        self.hub.session_start("agent-session-9", "h", 60)
        self.assertEqual([s["session"] for s in self.hub.sessions()["items"]], ["agent-session-9"])

    def test_session_end_drops_the_session_from_listing(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-4", "h", 60)
            self.hub.locker_save("agent-session-4", "role: implementer", holder="h")
            self.assertEqual([s["session"] for s in self.hub.sessions()["items"]],
                             ["agent-session-4"])
            self.hub.session_end("agent-session-4", "h")
            self.assertEqual(self.hub.sessions()["items"], [])
        self.hub.locker_save("agent-session-4", "prepared after end")
        self.assertEqual(self.hub.sessions()["items"], [])

    def test_sessions_idle_from_holder_writes_not_lease_renew(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-2", "h", 60)
            self.hub.locker_save("agent-session-2", "role: implementer", holder="h")
            self.hub.claim("RL-044", "agent-session-2", holder="h")
        with patch("hub.time.time", return_value=130):
            self.hub.session_start("agent-session-2", "h", 90)
            item = self.hub.sessions()["items"][0]
        self.assertEqual(item["active_at"], 100)
        self.assertEqual(item["idle_seconds"], 30)
        self.assertEqual(item["claims"], ["RL-044"])

    def test_integrator_vacancy_notice_once_after_300s_free(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-1", "int", 60)
            self.hub.session_start("agent-session-2", "impl", 60)
            self.hub.locker_save("agent-session-2", "role: implementer", holder="impl")
            self.hub.session_end("agent-session-1", "int")
        with patch("hub.time.time", return_value=400):
            self.hub.sessions()
            self.assertEqual(self._vacancy_messages(), [])
        with patch("hub.time.time", return_value=401):
            self.hub.sessions()
            first = self._vacancy_messages()
            self.assertEqual({m["recipient"] for m in first}, {"agent-session-2", "roman"})
            self.assertEqual({m["sender"] for m in first}, {"hub"})
            self.assertEqual({m["kind"] for m in first}, {"wake"})
            self.assertTrue(all("Pick up agent-session-1" in m["body"] for m in first))
            self.assertEqual([s["session"] for s in self.hub.sessions()["items"]],
                             ["agent-session-2"])
        with patch("hub.time.time", return_value=450):
            self.hub.sessions()
            self.assertEqual(self._vacancy_messages(), first)
        with patch("hub.time.time", return_value=500):
            self.hub.session_start("agent-session-1", "int2", 60)
            self.hub.session_end("agent-session-1", "int2")
        with patch("hub.time.time", return_value=800):
            self.hub.sessions()
            self.assertEqual(self._vacancy_messages(), first)
        with patch("hub.time.time", return_value=801):
            self.hub.sessions()
            second = self._vacancy_messages()
            self.assertEqual(len(second), 2 * len(first))

    def test_expired_integrator_lease_can_vacancy_without_session_end(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-1", "int", 60)
            self.hub.session_start("agent-session-2", "impl", 60)
        with patch("hub.time.time", return_value=460):
            self.hub.sessions()
            self.assertEqual(self._vacancy_messages(), [])
            self.assertEqual(sorted(s["session"] for s in self.hub.sessions()["items"]),
                             ["agent-session-1", "agent-session-2"])
        with patch("hub.time.time", return_value=461):
            self.hub.sessions()
            notices = self._vacancy_messages()
            self.assertTrue(notices)
            self.assertIn("agent-session-1", [s["session"] for s in self.hub.sessions()["items"]])

    def _vacancy_messages(self):
        with self.hub.connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE sender=? AND request_id GLOB ? ORDER BY id",
                ("hub", "integrator-vacancy-*")).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    unittest.main()
