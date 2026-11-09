// Offline unit tests for static/kit/fabric_worker.mjs (v1.1.0). Zero network calls.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Worker, VERSION } from "../static/kit/fabric_worker.mjs";

test("fabric_worker.mjs v1.1.0 mints suffix for my-bot-01, writes 0600 state, scopes X-Worker-Token, and runs v2 endpoints", async () => {
  assert.equal(VERSION, "1.1.0");
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "fw-mjs-"));
  const stateFile = path.join(tmpDir, ".fabric_worker_my-bot-01.json");
  const calls = [];

  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = new URL(url);
    const headers = Object.fromEntries(Object.entries(init.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url, origin: u.origin, pathname: u.pathname, search: u.search, headers, body });

    const reply = (status, obj) => ({
      status,
      headers: { get: () => null },
      text: async () => JSON.stringify(obj),
    });

    if (u.pathname.startsWith("/q/claim/")) {
      return reply(200, {
        ok: true,
        resolved_agent_id: "my-bot-01-9f8e7d",
        worker_token: "wt_mjs_1",
        lease: { task_id: "t_200", fencing_token: 4, title: "Inspect bridge deck" },
      });
    }
    if (u.pathname === "/q/checkpoint") {
      return reply(200, { ok: true, step_index: body.step_index, lease_extended: true });
    }
    if (u.pathname === "/q/submit") {
      return reply(200, {
        ok: true,
        agent_id: body.agent_id,
        completed_task_id: body.task_id,
        stored_task_objects: [{ object_key: body.task_objects[0].object_key }],
      });
    }
    if (u.pathname === "/q/frontier") {
      if ((init.method || "GET") === "GET") {
        const items = (u.searchParams.get("items") || "").split(",").filter(Boolean);
        return reply(200, { ok: true, agent_id: u.searchParams.get("agent_id"), unseen: items, newly_recorded: 0 });
      }
      if (body.auto_suffix) {
        return reply(200, {
          ok: true,
          resolved_agent_id: "my-bot-01-55aa66",
          worker_token: "wt_mjs_sweep",
          unseen: body.items,
          newly_recorded: body.items.length,
        });
      }
      return reply(200, { ok: true, unseen: body.items, newly_recorded: body.items.length });
    }
    if (u.pathname === "/q/discover") {
      return reply(200, { ok: true, unseen_by: u.searchParams.get("unseen_by"), items: [] });
    }
    if (u.pathname.startsWith("/q/resume/")) {
      return reply(200, {
        ok: true,
        token_matched: headers["x-worker-token"] === "wt_mjs_1",
        active_leases: [{ task_id: "t_201", fencing_token: 9, title: "Resumed task" }],
      });
    }
    if (u.pathname === "/q/next") {
      if (u.origin === "https://other.example") {
        return reply(200, { tasks: [], next: { action: "wait" }, leaked_wt: Boolean(headers["x-worker-token"]) });
      }
      return reply(503, { error: "busy" });
    }
    throw new Error(`unexpected URL ${url}`);
  };

  try {
    const w1 = new Worker("my-bot-01", {
      bases: ["https://shim.teleionlabs.com/q"],
      stateFile,
      sleep: async () => {},
      waits: [0],
    });
    const claimed = await w1.claim("t_200");
    assert.equal(claimed.lease.task_id, "t_200");
    assert.match(calls[0].search, /auto_suffix=1/);
    assert.equal(w1.effectiveId, "my-bot-01-9f8e7d");

    const mode = fs.statSync(stateFile).mode & 0o777;
    assert.equal(mode, 0o600);

    // Reload from disk under original my-bot-01 ID
    const w2 = new Worker("my-bot-01", {
      bases: ["https://shim.teleionlabs.com/q"],
      stateFile,
      sleep: async () => {},
      waits: [0],
    });
    const cp = await w2.checkpoint({ stepIndex: 2, totalSteps: 3, completedSteps: ["a", "b"], pendingSteps: ["c"], notes: "almost done" });
    assert.equal(cp.lease_extended, true);
    assert.equal(calls.at(-1).headers["x-worker-token"], "wt_mjs_1");
    assert.equal(calls.at(-1).body.agent_id, "my-bot-01-9f8e7d");

    const sub = await w2.submit("Completed deck inspection", { spans: 2 }, { strictContract: true });
    assert.equal(sub.completed_task_id, "t_200");
    assert.equal(calls.at(-1).body.strict_contract, true);
    assert.equal(w2.state.task_id, undefined);
    assert.equal(w2.state.worker_token, "wt_mjs_1");

    const frRo = await w2.frontier(["org/ro"], { record: false });
    assert.equal(frRo.newly_recorded, 0);
    assert.equal(calls.at(-1).body, null);

    const fr = await w2.frontier(["org/one", "org/two"], { record: true });
    assert.equal(fr.newly_recorded, 2);

    const disc = await w2.discover({ category: "video_as_code", unseenOnly: true });
    assert.equal(disc.unseen_by, "my-bot-01-9f8e7d");

    // Resume hydrates active_leases[0] when token_matched is true and no local lease is held
    const res = await w2.resume();
    assert.equal(res.token_matched, true);
    assert.equal(w2.state.task_id, "t_201");
    assert.equal(w2.state.fence, 9);

    // Sweep-first worker on placeholder ID absorbs resolved_agent_id + worker_token on POST /frontier
    const sweepFile = path.join(tmpDir, ".fabric_worker_sweep.json");
    const ws = new Worker("my-bot-01", {
      bases: ["https://shim.teleionlabs.com/q"],
      stateFile: sweepFile,
      sleep: async () => {},
      waits: [0],
    });
    await ws.frontier(["org/check"], { record: false });
    assert.equal(ws.state.resolved_agent_id, undefined);
    await ws.frontier(["org/check"], { record: true });
    assert.equal(ws.effectiveId, "my-bot-01-55aa66");
    assert.equal(ws.state.worker_token, "wt_mjs_sweep");

    // Cross-origin fallback never sends X-Worker-Token to another origin
    const w3 = new Worker("my-bot-01", {
      bases: ["https://shim.teleionlabs.com/q", "https://other.example/q"],
      stateFile,
      sleep: async () => {},
      waits: [0],
    });
    const nx = await w3.next({ unseenOnly: true });
    assert.equal(nx.leaked_wt, false);
    assert.match(calls.at(-1).search, /unseen_only=1/);
  } finally {
    globalThis.fetch = origFetch;
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
