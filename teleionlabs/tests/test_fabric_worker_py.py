#!/usr/bin/env python3
"""Offline unit tests for static/kit/fabric_worker.py (v1.1.0). Zero network calls."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
KIT_PY = HERE / "static" / "kit" / "fabric_worker.py"

spec = importlib.util.spec_from_file_location("fabric_worker", KIT_PY)
assert spec and spec.loader
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)


class FakeWorker(fw.Worker):
    def __init__(self, agent_id: str, handler, **kw):
        self._handler = handler
        self.calls: list[dict] = []
        super().__init__(agent_id, sleep=lambda _s: None, waits=(0.01,), **kw)

    def _once(self, base, method, path, body):
        wt = self.state.get("worker_token") or ""
        tok_origin = self.state.get("token_origin") or ""
        send_wt = bool(wt) and (not tok_origin or fw._origin_of(base) == tok_origin)
        headers = {
            "user-agent": f"{self.effective_id} fabric-worker/{fw.__version__}",
            "accept": "application/json",
            **({"content-type": "application/json"} if body is not None else {}),
            **({"x-worker-token": wt} if send_wt else {}),
        }
        self.calls.append({"base": base, "method": method, "path": path, "body": body, "headers": headers})
        status, payload = self._handler(base, method, path, body, headers)
        return status, json.dumps(payload), None


class FabricWorkerPyTests(unittest.TestCase):
    def test_placeholder_auto_suffix_and_0600_origin_scoped_wt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, ".fabric_worker_my-bot-01.json")

            def handler(base, method, path, body, headers):
                u = urllib.parse.urlsplit(path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path.startswith("/claim/"):
                    return 200, {
                        "ok": True,
                        "resolved_agent_id": "my-bot-01-a1b2c3",
                        "worker_token": "wt_short_1",
                        "lease": {"task_id": "t_100", "fencing_token": 7, "title": "Check water table"},
                    }
                if u.path == "/checkpoint":
                    return 200, {"ok": True, "step_index": body["step_index"], "lease_extended": True}
                if u.path == "/submit":
                    return 200, {
                        "ok": True,
                        "agent_id": body["agent_id"],
                        "completed_task_id": body["task_id"],
                        "stored_task_objects": [{"object_key": body["task_objects"][0]["object_key"]}],
                    }
                if u.path == "/frontier":
                    if method == "GET":
                        items = [x for x in q.get("items", "").split(",") if x]
                        return 200, {"ok": True, "agent_id": q.get("agent_id"), "unseen": items, "newly_recorded": 0}
                    if body.get("auto_suffix"):
                        return 200, {
                            "ok": True,
                            "resolved_agent_id": "my-bot-01-f9e8d7",
                            "worker_token": "wt_sweep_1",
                            "unseen": body["items"],
                            "newly_recorded": len(body["items"]),
                        }
                    return 200, {"ok": True, "unseen": body["items"], "newly_recorded": len(body["items"])}
                if u.path == "/discover":
                    return 200, {"ok": True, "unseen_by": q.get("unseen_by"), "items": []}
                if u.path.startswith("/resume/"):
                    return 200, {
                        "ok": True,
                        "token_matched": headers.get("x-worker-token") == "wt_short_1",
                        "active_leases": [{"task_id": "t_101", "fencing_token": 11, "title": "Resumed water check"}],
                    }
                if u.path == "/next":
                    if "other.example" in base:
                        return 200, {"tasks": [], "next": {"action": "wait"}, "leaked_wt": "x-worker-token" in headers}
                    return 503, {"error": "busy"}
                raise AssertionError(f"unexpected {path}")

            w = FakeWorker("my-bot-01", handler, bases=["https://shim.teleionlabs.com/q"], state_file=state_path)
            got = w.claim("t_100")
            self.assertEqual(got["lease"]["task_id"], "t_100")
            self.assertIn("auto_suffix=1", w.calls[0]["path"])
            self.assertEqual(w.effective_id, "my-bot-01-a1b2c3")

            mode = stat.S_IMODE(os.stat(state_path).st_mode)
            self.assertEqual(mode, 0o600)

            # Reload from disk under original --id my-bot-01: keeps resolved_agent_id + worker_token
            w2 = FakeWorker("my-bot-01", handler, bases=["https://shim.teleionlabs.com/q"], state_file=state_path)
            self.assertEqual(w2.effective_id, "my-bot-01-a1b2c3")

            cp = w2.checkpoint(step_index=1, total_steps=2, completed_steps=["collect"], pending_steps=["write"], notes="halfway")
            self.assertTrue(cp["lease_extended"])
            self.assertEqual(w2.calls[-1]["headers"].get("x-worker-token"), "wt_short_1")
            self.assertEqual(w2.calls[-1]["body"]["agent_id"], "my-bot-01-a1b2c3")

            sub = w2.submit("Finished water table summary", {"rows": 3}, strict_contract=True)
            self.assertEqual(sub["completed_task_id"], "t_100")
            self.assertTrue(w2.calls[-1]["body"]["strict_contract"])
            self.assertNotIn("task_id", w2.state)
            self.assertEqual(w2.state.get("worker_token"), "wt_short_1")

            # Frontier read-only uses GET (no name claim); record=True uses POST
            fr_ro = w2.frontier(["org/repo-ro"], record=False)
            self.assertEqual(w2.calls[-1]["method"], "GET")
            self.assertEqual(fr_ro["newly_recorded"], 0)
            fr = w2.frontier(["org/repo-a", "org/repo-b"], record=True)
            self.assertEqual(w2.calls[-1]["method"], "POST")
            self.assertEqual(fr["newly_recorded"], 2)
            disc = w2.discover(category="video_as_code", unseen_only=True)
            self.assertEqual(disc["unseen_by"], "my-bot-01-a1b2c3")
            res = w2.resume()
            self.assertTrue(res["token_matched"])
            self.assertEqual(w2.state.get("task_id"), "t_101")
            self.assertEqual(w2.state.get("fence"), 11)

            # Sweep-first worker on placeholder ID absorbs resolved_agent_id + worker_token on POST /frontier
            sweep_state = os.path.join(tmp, ".fabric_worker_sweep.json")
            ws = FakeWorker("my-bot-01", handler, bases=["https://shim.teleionlabs.com/q"], state_file=sweep_state)
            ws.frontier(["org/repo-x"], record=False)
            self.assertIsNone(ws.state.get("resolved_agent_id"))
            ws.frontier(["org/repo-x"], record=True)
            self.assertEqual(ws.effective_id, "my-bot-01-f9e8d7")
            self.assertEqual(ws.state.get("worker_token"), "wt_sweep_1")

            # Cross-origin fallback MUST NOT leak X-Worker-Token to another host
            w3 = FakeWorker(
                "my-bot-01",
                handler,
                bases=["https://shim.teleionlabs.com/q", "https://other.example/q"],
                state_file=state_path,
            )
            nx = w3.next(unseen_only=True)
            self.assertFalse(nx["leaked_wt"])
            self.assertIn("unseen_only=1", w3.calls[-1]["path"])


if __name__ == "__main__":
    unittest.main()
