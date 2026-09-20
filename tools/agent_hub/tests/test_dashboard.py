import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import serve
from hub import Hub, operator_key


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "hub.db"
        self.hub = Hub(self.db)
        for session in ("agent-session-1", "agent-session-2"):
            self.hub.locker_save(session, f"role: {session}")
        self.server, self.secret = serve(self.db, port=0, user="roman", integrator="agent-session-1")
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token=True, host=None, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Team-Token"] = self.secret
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_page_and_state_require_local_host_and_token(self):
        status, page = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Agent Hub Team", page)
        self.assertIn(b"Merge (0)", page)
        self.assertIn(b"Send with comments", page)
        self.assertIn(b"data-review-draft", page)
        self.assertIn(b"setSelectionRange", page)
        self.assertEqual(self.request("GET", "/", host="evil.example:80")[0], 403)
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 401)
        status, data = self.request("GET", "/api/state")
        state = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual((state["user"], state["integrator"]), ("roman", "agent-session-1"))
        self.assertTrue(state["board"]["tickets"])
        self.assertEqual([s["session"] for s in state["sessions"]], ["agent-session-1", "agent-session-2"])

    def test_chat_sends_as_the_user_to_one_or_all_sessions(self):
        status, data = self.request("POST", "/api/send", {"to": "agent-session-2", "body": "status?"})
        self.assertEqual(status, 200)
        message = json.loads(data)["sent"][0]
        self.assertEqual((message["sender"], message["recipient"]), ("roman", "agent-session-2"))
        self.request("POST", "/api/send", {"to": "all", "body": "stand-up in 5"})
        feed = json.loads(self.request("GET", "/api/messages?after=0")[1])["items"]
        self.assertEqual(sorted(m["recipient"] for m in feed if m["body"] == "stand-up in 5"),
                         ["agent-session-1", "agent-session-2"])

    def test_feature_requests_go_to_the_integrator(self):
        status, data = self.request("POST", "/api/features", {"title": "Telegram bridge", "body": "Later"})
        self.assertEqual(status, 200)
        request = json.loads(data)
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertEqual([(f["id"], f["status"]) for f in state["features"]], [(request["id"], "new")])
        self.assertIn("Telegram bridge", self.hub.inbox("agent-session-1")["items"][-1]["body"])

    def test_rejects_forms_bad_input_and_path_tricks(self):
        self.assertEqual(self.request("POST", "/api/send", {"to": "all", "body": "x"},
                                      content_type="text/plain")[0], 415)
        self.assertEqual(self.request("POST", "/api/send", {"to": "all", "body": "x"}, token=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/send", {"to": "bad id!", "body": "x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/features", {"title": ""})[0], 400)
        self.assertEqual(self.request("GET", "/api/handoff?ticket=../../AGENTS")[0], 400)
        status, data = self.request("GET", "/api/handoff?ticket=RL-012")
        self.assertEqual(status, 200)
        self.assertIn("RL-012", json.loads(data)["text"])

    def test_merge_queue_requires_clear_review_and_unchanged_head(self):
        sha = "f" * 40
        branches = [{"name": "feature/team-dashboard", "sha": sha, "time": 0, "subject": "Dashboard"}]
        self.hub.session_start("agent-session-1", "author-run")
        self.hub.session_start("agent-session-2", "review-run")
        self.hub.claim("RL-017", "agent-session-1", holder="author-run")
        with patch("dashboard._branches", return_value=branches), patch("dashboard._is_merged", return_value=False):
            self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["merge_queue"], [])
            self.hub.review_submit("RL-017", "feature/team-dashboard", sha, "agent-session-1",
                                   "agent-session-2", "One blocking issue", ["Fix the race"], [], "review-run")
            self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["merge_queue"], [])
            review = self.hub.review_submit("RL-017", "feature/team-dashboard", sha, "agent-session-1",
                                            "agent-session-2", "Ready after fixes", [],
                                            ["Naming nit remains"], "review-run")
            state = json.loads(self.request("GET", "/api/state")[1])
            self.assertEqual([(r["id"], r["comments"]) for r in state["merge_queue"]],
                             [(review["id"], ["Naming nit remains"])])
        moved = [{**branches[0], "sha": "e" * 40}]
        with patch("dashboard._branches", return_value=moved):
            self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["merge_queue"], [])
            self.assertEqual(self.request("POST", "/api/approve",
                                          {"review_id": review["id"], "sha": sha})[0], 409)
            self.assertEqual(self.request("POST", "/api/review-feedback",
                                          {"review_id": review["id"], "sha": sha,
                                           "body": "late note"})[0], 409)

        approve = {"review_id": review["id"], "sha": sha, "note": "reviewed"}
        with patch("dashboard._branches", return_value=branches), patch("dashboard._is_merged", return_value=False):
            self.assertEqual(self.request("POST", "/api/approve", approve, token=False)[0], 401)
            self.assertEqual(self.request("POST", "/api/approve", {**approve, "sha": "0" * 40})[0], 409)
            status, data = self.request("POST", "/api/approve", approve)
            self.assertEqual(status, 200)
            approval, key = json.loads(data), operator_key(self.db)
            self.assertEqual((approval["source"], approval["review_id"], approval["recorded_by"],
                              approval["recorded_holder"]),
                             ("dashboard", review["id"], "roman", ""))
            self.assertTrue(self.hub.approval("RL-017", sha, key)["approved"])
            self.assertIn("reviewed", self.hub.inbox("agent-session-1")["items"][-1]["body"])
            state = json.loads(self.request("GET", "/api/state")[1])
            self.assertEqual([(a["id"], a["status"], a["merged"]) for a in state["approvals"]],
                             [(approval["id"], "approved", False)])
            feedback = {"review_id": review["id"], "sha": sha, "body": "Please address the naming nit."}
            self.assertEqual(self.request("POST", "/api/review-feedback", feedback)[0], 200)
            self.assertFalse(self.hub.approval("RL-017", sha, key)["approved"])
            self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["merge_queue"], [])
            self.assertIn("naming nit", self.hub.inbox("agent-session-1")["items"][-1]["body"])
            self.assertEqual(self.request("POST", "/api/revoke", {"id": 999})[0], 400)

    def test_state_shows_idle_claims_drops_ended_and_notices_vacancy(self):
        with patch("hub.time.time", return_value=100):
            self.hub.session_start("agent-session-1", "int", 60)
            self.hub.session_start("agent-session-2", "impl", 60)
            self.hub.claim("RL-044", "agent-session-2", holder="impl")
            self.hub.session_end("agent-session-1", "int")
        with patch("hub.time.time", return_value=401):
            status, data = self.request("GET", "/api/state")
        state = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual([s["session"] for s in state["sessions"]], ["agent-session-2"])
        item = state["sessions"][0]
        self.assertEqual(item["claims"], ["RL-044"])
        self.assertEqual(item["idle_seconds"], 301)
        self.assertTrue(any("Pick up agent-session-1" in m["body"]
                            for m in self.hub.inbox("agent-session-2")["items"]))
        self.assertIn(b"idle ${Math.floor(s.idle_seconds)}s", self.request("GET", "/", token=False)[1])


if __name__ == "__main__":
    unittest.main()
