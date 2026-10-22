# SPDX-License-Identifier: Apache-2.0
"""Run with: python -m unittest discover -s deploy/huggingface/tests

The web page's JSON API (webapi.py). Hermetic: DataBounty's tools and public API are stubbed,
nothing touches the network.
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SPACE_PUBLIC_URL", "https://example-space.hf.space")
os.environ.setdefault("DATABOUNTY_MCP_URL", "https://console.example.test/mcp")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "databounty"))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - only where fastapi isn't installed
    FastAPI = None

if FastAPI is not None:
    import account
    import webapi

FIELDS = [
    {"key": "task", "label": "Task", "role": "instruction", "required": True, "help": "What to build."},
    {"key": "solution", "label": "Solution", "role": "solution_code", "required": True},
    {"key": "level", "label": "Level", "role": "enum", "required": False, "options": ["easy", "hard"]},
]
CONTRACT = {
    "bounty": {"title": "CLI parsers", "status": "active", "karmaPricing": {"contributorPerItem": 40}},
    "capacity": {"poolRemaining": 100, "recommendedPath": "direct_submit"},
    "submitLimits": {"maxItemsPerRequest": 2},
    "datasetType": {"name": "Python function", "fields": FIELDS},
}
ME = {"displayName": "Asha Rao", "handle": "asha", "email": "asha@example.test", "karmaTotal": 1240, "emailVerified": True, "onboarded": True}


@unittest.skipIf(FastAPI is None, "fastapi is not installed")
class WebApiTest(unittest.TestCase):
    def setUp(self):
        server = FastAPI()
        server.include_router(webapi.router)
        server.add_exception_handler(webapi.ApiError, webapi.api_error)
        self.client = TestClient(server)
        self.calls = []
        self.me = dict(ME)
        self.scope = "read contribute artifact account"
        self.submit_answer = None
        patcher = mock.patch.object(account, "call_tool", side_effect=self.call_tool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session_id = f"session-{id(self)}"
        session = account.Session(access_token="at", refresh_token=None, expires_at=time.time() + 3600, scope=self.scope)
        self.session = session
        with account._store_lock:
            account._sessions[account._digest(self.session_id)] = session
        self.addCleanup(lambda: account._sessions.pop(account._digest(self.session_id), None))
        self.auth = {"Authorization": f"Bearer {self.session_id}"}

    def call_tool(self, session, name, arguments=None):
        self.calls.append((name, arguments))
        if name == "whoami":
            return self.me
        if name == "suggest_handles":
            return ["asharao", "asha_r"]
        if name == "get_pool_contract":
            return CONTRACT
        if name == "submit_pool_items":
            if self.submit_answer:
                return self.submit_answer(arguments)
            return {"submissions": [{"id": f"s{i}", "status": "submitted"} for i, _ in enumerate(arguments["items"])]}
        if name == "list_my_submissions":
            return {"items": [{"id": "s1", "title": "Parse flags", "status": "tests_failed", "bounty": {"id": "p1", "title": "CLI parsers"}}], "total": 1}
        raise account.ToolError(f"{name} isn't stubbed")

    def sent(self, name):
        return [arguments for called, arguments in self.calls if called == name]

    # --- Signing in ---

    def test_a_finished_sign_in_is_claimed_once_with_its_secret(self):
        account.hold_sign_in(account._digest("the-secret"), "session-xyz")
        self.assertEqual(self.client.post("/api/session/claim", json={"nonce": "wrong"}).status_code, 404)
        res = self.client.post("/api/session/claim", json={"nonce": "the-secret"})
        self.assertEqual(res.json(), {"session": "session-xyz"})
        self.assertEqual(res.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post("/api/session/claim", json={"nonce": "the-secret"}).status_code, 404)

    def test_without_a_session_signed_in_routes_answer_401(self):
        for method, path in (("get", "/api/me"), ("get", "/api/submissions"), ("post", "/api/pools/p1/items")):
            with self.subTest(path):
                self.assertEqual(getattr(self.client, method)(path).status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_an_ended_sign_in_is_a_401_not_an_error_page(self):
        with mock.patch.object(account, "call_tool", side_effect=account.NotSignedIn("expired")):
            res = self.client.get("/api/me", headers=self.auth)
        self.assertEqual(res.status_code, 401)
        self.assertIn("Sign in again", res.json()["error"])

    def test_me_says_which_setup_step_is_next(self):
        cases = [
            ("ready", {}, None),
            ("finish_setup", {"onboarded": False}, None),
            ("choose_handle", {"handle": None}, None),
            ("verify_email", {"emailVerified": False}, None),
            ("permission", {}, "read account"),
        ]
        for step, changes, scope in cases:
            with self.subTest(step):
                self.me = {**ME, **changes}
                self.session.scope = scope or self.scope
                body = self.client.get("/api/me", headers=self.auth).json()
                self.assertEqual(body["step"], step)
        self.me = {**ME, "handle": None}
        self.session.scope = self.scope
        self.assertEqual(self.client.get("/api/me", headers=self.auth).json()["suggestedHandle"], "asharao")
        self.me = dict(ME)
        body = self.client.get("/api/me", headers=self.auth).json()
        self.assertEqual((body["karma"], body["handle"]), (1240, "asha"))
        self.assertTrue(body["profileUrl"].endswith("/asha/"))

    # --- One item ---

    def submit(self, values, method="human", title=""):
        data = {"values": json.dumps(values), "method": method, "title": title}
        return self.client.post("/api/pools/p1/items", data=data, headers=self.auth)

    def test_an_item_goes_to_databounty_with_its_title_and_method(self):
        res = self.submit({"task": "  Parse   flags ", "solution": "def f(): pass", "level": "hard"}, method="ai_assisted")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["duplicate"], False)
        (sent,) = self.sent("submit_pool_items")
        self.assertEqual(sent["bountyId"], "p1")
        self.assertEqual(
            sent["items"],
            # The title is tidied; the item itself keeps its spacing, which can matter.
            [{"title": "Parse flags", "payloadJson": {"task": "Parse   flags", "solution": "def f(): pass", "level": "hard"}, "generationMethod": "ai_assisted"}],
        )

    def test_an_item_that_would_be_refused_is_not_sent(self):
        cases = {
            "no method": ({"task": "t", "solution": "s"}, "", "Choose how you made this item."),
            "missing field": ({"task": "t"}, "human", "Fill in: Solution."),
            "wrong option": ({"task": "t", "solution": "s", "level": "medium"}, "human", "Level must be one of 'easy', 'hard'"),
        }
        for name, (values, method, message) in cases.items():
            with self.subTest(name):
                res = self.submit(values, method=method)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.json()["error"], message)
        self.assertEqual(self.sent("submit_pool_items"), [])

    def test_a_duplicate_is_reported_as_one(self):
        self.submit_answer = lambda _: {"submissions": [{"id": "s1", "status": "rejected", "duplicateOfSubmissionId": "s0"}]}
        self.assertTrue(self.submit({"task": "t", "solution": "s"}).json()["duplicate"])

    # --- Many items ---

    def test_a_file_is_previewed_row_by_row(self):
        csv = "task,solution,level\nParse flags,def f(): pass,easy\nNo solution,,\nBad level,x,medium\n"
        res = self.client.post("/api/pools/p1/bulk/preview", files={"file": ("items.csv", csv, "text/csv")}, headers=self.auth)
        self.assertEqual(res.status_code, 200, res.text)
        rows = res.json()["rows"]
        self.assertEqual([(r["number"], r["ok"]) for r in rows], [(1, True), (2, False), (3, False)])
        self.assertEqual(rows[0]["title"], "Parse flags")
        self.assertIn("solution", rows[1]["problem"])
        self.assertEqual(self.sent("submit_pool_items"), [])

    def test_sending_checks_every_row_again_and_sends_in_batches(self):
        good = [{"number": n, "title": f"Item {n}", "payload": {"task": f"t{n}", "solution": "s"}} for n in (1, 2, 3)]
        tampered = {"number": 4, "title": "Sneaky", "payload": {"task": "t4"}}
        res = self.client.post("/api/pools/p1/bulk/send", json={"method": "human", "rows": good + [tampered]}, headers=self.auth)
        self.assertEqual(res.json(), {"sent": 3, "duplicates": 0, "skipped": 1, "problem": "", "karma": 40})
        batches = self.sent("submit_pool_items")
        self.assertEqual([len(b["items"]) for b in batches], [2, 1])  # the pool's maxItemsPerRequest is 2
        self.assertEqual(batches[0]["items"][0]["generationMethod"], "human")

    def test_sending_stops_when_the_pool_fills(self):
        self.submit_answer = lambda args: {"submissions": [{"id": "s1", "status": "submitted"}], "stoppedAtCapacity": True}
        rows = [{"number": n, "payload": {"task": f"t{n}", "solution": "s"}} for n in (1, 2, 3)]
        body = self.client.post("/api/pools/p1/bulk/send", json={"method": "human", "rows": rows}, headers=self.auth).json()
        self.assertEqual((body["sent"], body["problem"]), (1, "The pool filled up, so the rest weren't sent."))
        self.assertEqual(len(self.sent("submit_pool_items")), 1)

    # --- Reading ---

    def test_submissions_come_back_ready_to_draw(self):
        body = self.client.get("/api/submissions", headers=self.auth).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(
            {k: body["items"][0][k] for k in ("title", "pool", "statusLabel", "statusTone", "statusGroup")},
            {"title": "Parse flags", "pool": "CLI parsers", "statusLabel": "Needs your fix", "statusTone": "warn", "statusGroup": "action"},
        )

    def test_a_pool_reads_publicly_until_someone_signs_in(self):
        details = {"id": "p1", "title": "CLI parsers", "status": "active", "target_items": 10, "accepted_items": 4, "fields": FIELDS}
        with mock.patch.object(webapi.db, "pool_details", return_value=details):
            public = self.client.get("/api/pools/p1/contract").json()
        self.assertEqual((public["signedIn"], public["remaining"]), (False, 6))
        signed_in = self.client.get("/api/pools/p1/contract", headers=self.auth).json()
        self.assertEqual((signed_in["signedIn"], signed_in["remaining"], signed_in["maxItems"]), (True, 100, 2))
        with mock.patch.object(webapi.db, "pool_details", return_value=None):
            self.assertEqual(self.client.get("/api/pools/gone/contract").status_code, 404)

    def test_the_template_is_a_csv_download(self):
        with mock.patch.object(webapi.db, "pool_details", return_value={"id": "p1", "title": "CLI Parsers!", "fields": FIELDS}):
            res = self.client.get("/api/pools/p1/template.csv")
        self.assertEqual(res.text.strip(), "task,solution,level")
        self.assertEqual(res.headers["content-disposition"], 'attachment; filename="cli-parsers-template.csv"')

    def test_a_public_api_outage_is_a_502_with_a_message(self):
        with mock.patch.object(webapi.db, "published_datasets", side_effect=webapi.db.UpstreamError("DataBounty didn't answer.")):
            res = self.client.get("/api/datasets")
        self.assertEqual((res.status_code, res.json()), (502, {"error": "DataBounty didn't answer."}))


if __name__ == "__main__":
    unittest.main()
