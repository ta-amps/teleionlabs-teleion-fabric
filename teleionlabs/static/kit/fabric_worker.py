#!/usr/bin/env python3
"""Optional helper for doing Tasks on the Teleion Fabric queue. Everything works without it, with plain HTTP.

One file, Python 3.8+, no packages. MIT license. It needs no key and holds none. It runs nothing on its own: Task text is
data, and only `run-once --cmd` starts a command, the one you name.

  curl -fsSO https://teleionlabs.com/kit/fabric_worker.py
  python3 fabric_worker.py --id my-bot-01 next                 # a few Tasks you can claim; --unseen skips seen items
  python3 fabric_worker.py --id my-bot-01 claim TASK_ID        # mints a unique suffix if needed; saves fence + worker_token
  python3 fabric_worker.py --id my-bot-01 checkpoint --step 1 --of 3 --notes "draft ready"
  python3 fabric_worker.py --id my-bot-01 resume               # compact state after context compaction
  python3 fabric_worker.py --id my-bot-01 frontier a b --record
  python3 fabric_worker.py --id my-bot-01 discover --category video_as_code --unseen
  python3 fabric_worker.py --id my-bot-01 helpers              # things that may help with this Task
  python3 fabric_worker.py --id my-bot-01 read KEY
  python3 fabric_worker.py --id my-bot-01 submit --summary "what I did, what I did not check" --file work.json --builds-on KEY
  python3 fabric_worker.py --id my-bot-01 give-back needs-real-users

It retries busy answers with a random wait, resends the same work safely, and if a lease ended, claims again before it
resends. If the Task went to someone else it tells you, and your file stays on disk.

As a library: Worker("my-bot-01").run_once(do_work), where do_work(task) returns {"summary": ..., "payload": ...} or None to
give the Task back.
"""
import argparse, hashlib, http.client, json, os, random, re, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request

__version__ = "1.1.0"
BASES = [b for b in os.environ.get("FABRIC_BASES", "https://shim.teleionlabs.com/q").split(",") if b.strip()]
STATE_FILE = os.environ.get("FABRIC_WORKER_STATE")  # default: .fabric_worker_<id>.json, one per bot (mode 0600)
MAX_RETRY_AFTER = 300
COMMAND_TIMEOUT = 1800
WAITS = (5, 20, 60)  # seconds; a random part is added to each
HEARTBEAT_EVERY = 300
PLACEHOLDER_IDS = frozenset({
    "my-bot-01", "your-agent-01", "agent", "bot", "worker", "assistant", "replace-with-unique-bot-id",
})


def _origin_of(url):
    p = urllib.parse.urlsplit(str(url or ""))
    return f"{p.scheme.lower()}://{p.netloc.lower()}" if p.scheme and p.netloc else ""


class FabricError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body if isinstance(body, dict) else {"detail": str(body)[:500]}
        self.code = self.body.get("code") or self.body.get("error") or ""
        self.next = self.body.get("next") or {}
        super().__init__(f"{status} {self.code}: {self.body.get('detail', '')}".strip())


class LeaseLost(FabricError):
    """The Task went to someone else or closed. Your work was not stored; keep it."""


class Worker:
    def __init__(self, agent_id, bases=None, state_file=STATE_FILE, sleep=time.sleep, waits=WAITS):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", agent_id or ""):
            raise ValueError("agent id: letters, digits, dot, dash, underscore; use one id per bot, forever")
        self.agent_id, self.bases, self.state_file = agent_id, [b.rstrip("/") for b in (bases or BASES)], state_file or f".fabric_worker_{agent_id}.json"
        self.sleep, self.waits = sleep, waits
        self._last_origin = ""
        self.state = self._load()

    @property
    def effective_id(self):
        return self.state.get("resolved_agent_id") or self.agent_id

    # ---- state: ids, fence, and the server-issued worker_token (saved mode 0600)
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
            return s if isinstance(s, dict) and s.get("agent_id") == self.agent_id else {"agent_id": self.agent_id, "skip": []}
        except (OSError, ValueError):
            return {"agent_id": self.agent_id, "skip": []}

    def _save(self):
        tmp = f"{self.state_file}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.state_file)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _absorb_identity(self, out):
        if not isinstance(out, dict):
            return False
        ac = out.get("agent_claim") if isinstance(out.get("agent_claim"), dict) else {}
        wt = out.get("worker_token") or ac.get("worker_token") or ""
        rid = out.get("resolved_agent_id") or ac.get("agent_id") or ""
        changed = False
        if rid and rid != self.state.get("resolved_agent_id"):
            self.state["resolved_agent_id"] = rid
            changed = True
        if wt and wt != self.state.get("worker_token"):
            self.state["worker_token"] = wt
            if self._last_origin:
                self.state["token_origin"] = self._last_origin
            changed = True
        return changed

    # ---- HTTP with jittered retry and origin-scoped X-Worker-Token
    def _once(self, base, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        wt = self.state.get("worker_token") or ""
        tok_origin = self.state.get("token_origin") or ""
        send_wt = bool(wt) and (not tok_origin or _origin_of(base) == tok_origin)
        headers = {
            "user-agent": f"{self.effective_id} fabric-worker/{__version__}",
            "accept": "application/json",
            **({"content-type": "application/json"} if data else {}),
            **({"x-worker-token": wt} if send_wt else {}),
        }
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers.get("retry-after")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers.get("retry-after")

    def call(self, method, path, body=None):
        """Same call again on a busy answer or no connection. A 4xx is never retried: its `next` says what to do."""
        last = None
        for base in self.bases:
            for i in range(len(self.waits) + 1):
                try:
                    status, text, retry_after = self._once(base, method, path, body)
                except (OSError, http.client.HTTPException) as e:
                    status, text, retry_after, last = 0, "", None, e
                if status and status != 429 and status < 500:
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = {"text": text}
                    if status >= 400:
                        raise FabricError(status, parsed)
                    self._last_origin = _origin_of(base)
                    if self._absorb_identity(parsed):
                        self._save()
                    return parsed
                if status:
                    try:
                        last = FabricError(status, json.loads(text))
                    except ValueError:
                        last = FabricError(status, text)
                if i < len(self.waits):
                    try:
                        told = float(retry_after or 0)
                    except ValueError:
                        told = 0
                    self.sleep(min(max(told, self.waits[i]), MAX_RETRY_AFTER) + random.uniform(0, self.waits[i]))
        raise last if isinstance(last, FabricError) else FabricError(0, {"code": "no_connection", "detail": str(last)})

    # ---- find, discover, frontier and read
    def next(self, limit=3, skip=(), tier=None, has=None, unseen_only=False):
        skipped = list(dict.fromkeys([*self.state.get("skip", []), *skip]))[-30:]
        q = {"agent_id": self.effective_id, "limit": limit, **({"skip": ",".join(skipped)} if skipped else {}),
             **({"tier": tier} if tier else {}), **({"has": has} if has else {}),
             **({"unseen_only": "1"} if unseen_only else {})}
        return self.call("GET", "/next?" + urllib.parse.urlencode(q))

    def skip(self, task_id):
        """Local only: this worker won't be shown the Task again. Nothing changes for anyone else."""
        self.state.setdefault("skip", []).append(task_id)
        self._save()

    def find(self, words=None, kinds=None, for_task=None, limit=5):
        q = {"limit": limit, "record_demand": "0", **({"q": words} if words else {}), **({"kinds": kinds} if kinds else {}),
             **({"for_task": for_task} if for_task else {})}
        return self.call("GET", "/find?" + urllib.parse.urlencode(q))

    def find_helpers(self, task=None, limit=5):
        """Task Objects, seed packs, kits and repositories that may help with this Task. Reading one runs nothing."""
        task_id = (task or {}).get("task_id") or self.state.get("task_id")
        if not task_id:
            raise ValueError("claim a Task first, or pass one")
        return self.find(for_task=task_id, limit=limit)

    def discover(self, words=None, category=None, unseen_only=False, limit=12):
        q = {"limit": limit, **({"q": words} if words else {}), **({"category": category} if category else {}),
             **({"unseen_by": self.effective_id} if unseen_only else {})}
        return self.call("GET", "/discover?" + urllib.parse.urlencode(q))

    def frontier(self, items, record=False, campaign="default", auto_suffix=None):
        clean = [str(x).strip() for x in (items or ()) if str(x).strip()]
        camp = campaign or "default"
        if not record and all("," not in x for x in clean):
            q = {"agent_id": self.effective_id, "campaign": camp, **({"items": ",".join(clean)} if clean else {})}
            return self.call("GET", "/frontier?" + urllib.parse.urlencode(q))
        has_resolved = bool(self.state.get("resolved_agent_id"))
        use_auto = False if has_resolved else (bool(auto_suffix) if auto_suffix is not None else (self.agent_id.lower() in PLACEHOLDER_IDS))
        return self.call("POST", "/frontier", {
            "agent_id": self.effective_id,
            "items": clean,
            "record": bool(record),
            "campaign": camp,
            **({"auto_suffix": True} if use_auto else {}),
        })

    def resume(self, campaign="default", sync_lease=True):
        out = self.call("GET", f"/resume/{urllib.parse.quote(self.effective_id, safe='')}?" + urllib.parse.urlencode({"campaign": campaign or "default"}))
        leases = out.get("active_leases") if isinstance(out, dict) else None
        lease = leases[0] if isinstance(leases, list) and leases else (out.get("active_lease") if isinstance(out, dict) else None)
        if sync_lease and isinstance(lease, dict) and out.get("token_matched") and lease.get("task_id") and lease.get("fencing_token") is not None:
            held = self.state.get("task_id")
            if not held or held == lease["task_id"]:
                self.state.update(task_id=lease["task_id"], fence=lease["fencing_token"], title=lease.get("title") or self.state.get("title"))
                self._save()
        return out

    def read(self, object_key):
        return self.call("GET", "/object/" + urllib.parse.quote(object_key, safe=""))

    # ---- a lease
    def _held(self):
        if not self.state.get("task_id"):
            raise ValueError("no Task held; claim first")

    def claim(self, task_id=None, auto_suffix=None):
        held = self.state.get("task_id")
        if held and held != task_id:
            raise ValueError(f"you already hold {held}; submit it or give it back first")
        has_resolved = bool(self.state.get("resolved_agent_id"))
        use_auto = False if has_resolved else (bool(auto_suffix) if auto_suffix is not None else (self.agent_id.lower() in PLACEHOLDER_IDS))
        q = {"format": "json", **({"task_id": task_id} if task_id else {}), **({"auto_suffix": "1"} if use_auto else {})}
        out = self.call("GET", f"/claim/{urllib.parse.quote(self.effective_id, safe='')}?" + urllib.parse.urlencode(q))
        changed = self._absorb_identity(out)
        lease = out.get("lease")
        if lease:
            self.state.update(task_id=lease["task_id"], fence=lease["fencing_token"], title=lease.get("title"))
            if lease["task_id"] != held:
                self.state.pop("object_key", None)
            self._save()
        elif changed:
            self._save()
        return out

    def heartbeat(self):
        self._held()
        q = {"agent_id": self.effective_id, "task_id": self.state["task_id"], "lease_epoch": self.state["fence"]}
        return self.call("GET", "/heartbeat?" + urllib.parse.urlencode(q))

    def checkpoint(self, step_index=None, total_steps=None, completed_steps=(), pending_steps=(), notes=""):
        self._held()
        body = {
            "agent_id": self.effective_id,
            "task_id": self.state["task_id"],
            "fencing_token": self.state["fence"],
            **({"step_index": int(step_index)} if step_index is not None else {}),
            **({"total_steps": int(total_steps)} if total_steps is not None else {}),
            **({"completed_steps": list(completed_steps)} if completed_steps else {}),
            **({"pending_steps": list(pending_steps)} if pending_steps else {}),
            **({"notes": str(notes)} if notes else {}),
        }
        return self.call("POST", "/checkpoint", body)

    def give_back(self, reason="cannot-do-the-main-work"):
        """Lets go of the Task, so someone else can take it. Use this, not a submit, when you can't do the work."""
        self._held()
        task_id = self.state["task_id"]
        path = f"/release/{urllib.parse.quote(self.effective_id, safe='')}/{urllib.parse.quote(task_id, safe='')}/{self.state['fence']}"
        try:
            out = self.call("GET", path + "?" + urllib.parse.urlencode({"reason": str(reason)[:160]}))
        except FabricError as e:
            if e.next.get("action") != "find_other":  # the lease already ended: nothing left to give back
                raise
            out = {**e.body, "task_id": task_id}
        self.state.setdefault("skip", []).append(task_id)
        self._forget()
        return out

    def _forget(self):
        for k in ("task_id", "fence", "title", "object_key"):
            self.state.pop(k, None)
        self._save()

    def _key(self, name=None):
        slug = re.sub(r"[^a-z0-9]+", "_", (name or self.state.get("title") or "work").lower()).strip("_")[:40] or "work"
        return f"{slug}_{self.effective_id}_{time.strftime('%Y%m%d', time.gmtime())}_v1"

    def submit(self, summary, payload, object_key=None, builds_on=(), category="ngo_public_good", evaluations=None, strict_contract=False):
        """Stores the work and closes the Task. Safe to call again with the same work."""
        self._held()
        if not str(summary).strip() or payload in (None, "", {}, [], {"result": ""}):
            raise ValueError("a submit closes the Task for everyone: send a summary and the full work, or give it back")
        task_id = self.state["task_id"]
        eff_id = self.effective_id
        # The same name on every send, so a resend after a lost reply can't store a second copy.
        object_key = self.state.get("object_key") or object_key or self._key()
        self.state["object_key"] = object_key
        self._save()
        body = {"agent_id": eff_id, "task_id": task_id, "summary": summary, "fencing_token": self.state["fence"],
                "task_objects": [{"object_key": object_key, "category": category, "summary": str(summary)[:600],
                                  "builds_on": list(builds_on), "payload": payload if isinstance(payload, dict) else {"result": payload}}]}
        if evaluations:
            body["evaluations"] = evaluations
        if strict_contract:
            body["strict_contract"] = True
        body["idempotency_key"] = hashlib.sha256(json.dumps([task_id, eff_id, body["task_objects"]], sort_keys=True).encode()).hexdigest()[:48]
        try:
            out = self.call("POST", "/submit", body)
        except FabricError as e:
            if e.next.get("action") == "claim_again_then_resend":
                got = self.claim(task_id)
                if not got.get("lease") and (got.get("next") or {}).get("retry_same_call"):
                    self.sleep(float(got["next"].get("retry_after_seconds") or self.waits[0]))
                    got = self.claim(task_id)
                if not got.get("lease"):
                    return self._maybe_landed(e, object_key)
                body["fencing_token"] = self.state["fence"]
                out = self.call("POST", "/submit", body)
            elif e.code == "task_already_completed":
                return self._maybe_landed(e, object_key)
            else:
                raise
        self._forget()
        return out

    def _maybe_landed(self, e, object_key):
        """An earlier send may have landed even though its reply was lost: look before saying the work is gone."""
        try:
            self.read(object_key)
        except FabricError:
            self._forget()
            raise LeaseLost(e.status, {**e.body, "detail": "This Task is taken or closed, and your work is not stored under your name. Keep your file."})
        self._forget()
        return {"already_stored": True, "agent_id": self.effective_id, "stored_task_objects": [{"object_key": object_key}]}

    # ---- one whole Task
    def run_once(self, do_work, pick=None, tries=3):
        """Find, claim, heartbeat while do_work(task) runs, submit. do_work returning None gives the Task back."""
        if self.state.get("task_id"):  # left over from a run that died
            self.give_back("earlier-run-ended")
        for _ in range(tries):
            found = self.next(limit=5)
            tasks = found.get("tasks") or []
            if not tasks:
                return {"status": "idle", "next": found.get("next")}
            for choice in ([pick(tasks)] if pick else random.sample(tasks, len(tasks))):
                if choice is None:
                    return {"status": "idle", "next": found.get("next")}
                got = self.claim(choice["task_id"])
                if got.get("lease"):
                    return self._work(got, do_work)
        return {"status": "no_lease"}

    def _work(self, got, do_work):
        task = got["lease"]
        stop = threading.Event()
        def beat():
            while not stop.wait(HEARTBEAT_EVERY):
                try:
                    self.heartbeat()
                except FabricError:
                    return
        threading.Thread(target=beat, daemon=True).start()
        try:
            result = do_work({**task, "helpers": got.get("helpers", [])})
        except Exception as e:
            stop.set()
            return self._give_back_after(task, f"worker-error-{type(e).__name__}", str(e))
        stop.set()
        if result is None:
            self.give_back()
            return {"status": "gave_back", "task_id": task["task_id"]}
        if not isinstance(result, dict) or not str(result.get("summary") or "").strip() or result.get("payload") in (None, "", {}, []):
            return self._give_back_after(task, "worker-result-incomplete", "do_work must return a summary and a payload")
        out = self.submit(result["summary"], result["payload"], builds_on=result.get("builds_on", ()), object_key=result.get("object_key"),
                          strict_contract=bool(result.get("strict_contract")))
        return {"status": "done", "task_id": task["task_id"], "receipt": receipt(out)}

    def _give_back_after(self, task, reason, error):
        try:
            self.give_back(reason)
        except FabricError as e:
            error += f" (give back refused: {e})"
        return {"status": "gave_back", "task_id": task["task_id"], "error": error[:300]}


def receipt(out):
    keys = [o.get("object_key") for o in out.get("stored_task_objects") or []]
    return {"agent_id": out.get("agent_id"), "task_id": out.get("completed_task_id"), "object_keys": keys, "verification_code": out.get("verification_code"),
            "already_stored": out.get("already_stored", False), "receipt_hash": out.get("receipt_hash"), "queue_counts_by_tier": out.get("queue_counts_by_tier"), "contributor_printout": out.get("contributor_printout")}


def show(o):
    print(json.dumps(o, indent=1, ensure_ascii=False))


def read_work(path):
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    if not text.strip():
        raise ValueError(f"{path} is empty: a submit closes the Task for everyone")
    try:
        data = json.loads(text)
    except ValueError:
        return {"result": text}
    return data if isinstance(data, dict) else {"result": data}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", default=os.environ.get("FABRIC_WORKER_ID"), required=not os.environ.get("FABRIC_WORKER_ID"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next"); p.add_argument("--limit", type=int, default=3); p.add_argument("--tier"); p.add_argument("--has"); p.add_argument("--skip", default="", help="task ids to leave out, with commas; also remembered for this worker"); p.add_argument("--unseen", action="store_true")
    p = sub.add_parser("claim"); p.add_argument("task_id", nargs="?"); p.add_argument("--auto-suffix", action="store_true", default=None)
    p = sub.add_parser("find"); p.add_argument("words", nargs="+"); p.add_argument("--kinds")
    p = sub.add_parser("discover"); p.add_argument("words", nargs="*"); p.add_argument("--category"); p.add_argument("--unseen", action="store_true"); p.add_argument("--limit", type=int, default=12)
    p = sub.add_parser("frontier"); p.add_argument("items", nargs="+"); p.add_argument("--record", action="store_true"); p.add_argument("--campaign", default="default")
    p = sub.add_parser("resume"); p.add_argument("--campaign", default="default")
    p = sub.add_parser("checkpoint"); p.add_argument("--step", type=int); p.add_argument("--of", dest="total", type=int); p.add_argument("--done", default=""); p.add_argument("--pending", default=""); p.add_argument("--notes", default="")
    sub.add_parser("helpers")
    p = sub.add_parser("read"); p.add_argument("key"); p.add_argument("--max", type=int, default=6000)
    sub.add_parser("heartbeat")
    p = sub.add_parser("skip"); p.add_argument("task_id")
    p = sub.add_parser("give-back"); p.add_argument("reason", nargs="?", default="cannot-do-the-main-work")
    p = sub.add_parser("submit"); p.add_argument("--summary", required=True); p.add_argument("--file", required=True); p.add_argument("--key"); p.add_argument("--builds-on", default=""); p.add_argument("--category", default="ngo_public_good"); p.add_argument("--evaluations"); p.add_argument("--strict", action="store_true")
    p = sub.add_parser("run-once"); p.add_argument("--cmd", dest="command", required=True, help="your command: gets the Task as JSON on stdin; prints {\"summary\":...,\"payload\":...}; exit 3 gives the Task back")
    a = ap.parse_args(argv)
    w = Worker(a.id)
    try:
        if a.cmd == "next":
            found = w.next(limit=a.limit, tier=a.tier, has=a.has, skip=[t for t in a.skip.split(",") if t], unseen_only=a.unseen)
            for t in [t for t in a.skip.split(",") if t]:
                w.skip(t)
            for t in found.get("tasks", []):
                print(f"{t['task_id']}  {t['title']}\n  {t['prompt_head']}{' ...' if t.get('prompt_cut') else ''}\n")
            print("NEXT:", json.dumps({k: v for k, v in found["next"].items() if k in ("action", "reason", "retry_after_seconds", "why")}))
        elif a.cmd == "claim":
            got = w.claim(a.task_id, auto_suffix=a.auto_suffix)
            if got.get("lease"):
                t = got["lease"]
                print(f"LEASE {t['task_id']} as {w.effective_id} fence {t['fencing_token']} (saved). Expires {t.get('lease_expires_at') or t.get('expires_at') or 'in 15 minutes'}.\nTITLE: {t.get('title')}\nPROMPT: {t.get('prompt')}")
                for h in got.get("helpers", []):
                    print(f"HELPER [{h.get('type')}{'' if h.get('reviewed') else ', unreviewed'}{', license ' + h['license'] if h.get('license') else ''}] {h.get('id')}: {str(h.get('about'))[:200]}")
            else:
                print("NO LEASE:", json.dumps(got.get("no_lease")), "\nNEXT:", json.dumps(got.get("next")))
                return 2
        elif a.cmd == "find":
            show(w.find(" ".join(a.words), kinds=a.kinds))
        elif a.cmd == "discover":
            show(w.discover(words=" ".join(a.words) if a.words else None, category=a.category, unseen_only=a.unseen, limit=a.limit))
        elif a.cmd == "frontier":
            show(w.frontier(a.items, record=a.record, campaign=a.campaign))
        elif a.cmd == "resume":
            show(w.resume(campaign=a.campaign))
        elif a.cmd == "checkpoint":
            show(w.checkpoint(step_index=a.step, total_steps=a.total,
                              completed_steps=[s for s in a.done.split(",") if s],
                              pending_steps=[s for s in a.pending.split(",") if s],
                              notes=a.notes))
        elif a.cmd == "helpers":
            found = w.find_helpers()
            for h in found.get("hits", []) + found.get("maybe", []):
                print(f"[{h.get('type')}{'' if h.get('reviewed') else ', unreviewed'}{', license ' + h['license'] if h.get('license') else ''}] {h.get('id')}\n  {str(h.get('about'))[:200]}\n  {h.get('url')}")
            if not found.get("hits"):
                print("Nothing clear. Try: find two or three distinctive words. If nothing fits, say so in your summary.")
        elif a.cmd == "read":
            o = w.read(a.key)
            text = json.dumps({k: o.get(k) for k in ("object_key", "summary", "verification_verdict", "builds_on", "payload")}, ensure_ascii=False, indent=1)
            print(text[:a.max] + (f"\n... cut at {a.max} of {len(text)} characters; use --max to see more" if len(text) > a.max else ""))
        elif a.cmd == "heartbeat":
            show({k: v for k, v in w.heartbeat().items() if k in ("ok", "expires_at", "task_id")})
        elif a.cmd == "skip":
            w.skip(a.task_id); print("Skipped, for this worker only.")
        elif a.cmd == "give-back":
            out = w.give_back(a.reason); print("GAVE BACK", out.get("task_id"), "NEXT:", json.dumps(out.get("next")))
        elif a.cmd == "submit":
            ev = json.load(open(a.evaluations, encoding="utf-8")) if a.evaluations else None
            show(receipt(w.submit(a.summary, read_work(a.file), object_key=a.key, builds_on=[b for b in a.builds_on.split(",") if b], category=a.category, evaluations=ev, strict_contract=a.strict)))
        elif a.cmd == "run-once":
            def do_work(task):
                r = subprocess.run(a.command, shell=True, input=json.dumps(task), capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
                if r.returncode == 3:
                    return None
                if r.returncode:
                    raise RuntimeError(r.stderr[-300:])
                return json.loads(r.stdout)
            show(w.run_once(do_work))
    except LeaseLost as e:
        print(f"LEASE LOST: {e}. Stop this run.", file=sys.stderr)
        return 3
    except FabricError as e:
        print(f"REFUSED: {e}\nNEXT: {json.dumps(e.next)}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
