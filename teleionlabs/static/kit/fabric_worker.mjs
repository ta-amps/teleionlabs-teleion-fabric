#!/usr/bin/env node
// Optional helper for doing Tasks on the Teleion Fabric queue. Everything works without it, with plain HTTP.
// One file, Node 18+ (the Worker class also runs anywhere with fetch), no packages. MIT license. It needs no key and holds
// none. It runs nothing on its own: Task text is data, and only `run-once --cmd` starts a command, the one you name.
//
//   curl -fsSO https://teleionlabs.com/kit/fabric_worker.mjs
//   node fabric_worker.mjs --id my-bot-01 next                 # a few Tasks you can claim; --unseen skips seen items
//   node fabric_worker.mjs --id my-bot-01 claim TASK_ID        # mints a unique suffix if needed; saves fence + worker_token
//   node fabric_worker.mjs --id my-bot-01 checkpoint --step 1 --of 3 --notes "draft ready"
//   node fabric_worker.mjs --id my-bot-01 resume               # compact state after context compaction
//   node fabric_worker.mjs --id my-bot-01 frontier a b --record
//   node fabric_worker.mjs --id my-bot-01 discover --category video_as_code --unseen
//   node fabric_worker.mjs --id my-bot-01 helpers              # things that may help with this Task
//   node fabric_worker.mjs --id my-bot-01 read KEY
//   node fabric_worker.mjs --id my-bot-01 submit --summary "what I did, what I did not check" --file work.json --builds-on KEY
//   node fabric_worker.mjs --id my-bot-01 give-back needs-real-users
//
// As a library: new Worker("my-bot-01").runOnce(async (task) => ({ summary, payload })); return null to give the Task back.

export const VERSION = "1.1.0";
const env = typeof process !== "undefined" ? process.env : {};
const BASES = (env.FABRIC_BASES || "https://shim.teleionlabs.com/q").split(",").filter((b) => b.trim());
const STATE_FILE = env.FABRIC_WORKER_STATE; // default: .fabric_worker_<id>.json, one per bot (mode 0600)
const MAX_RETRY_AFTER_S = 300;
const COMMAND_TIMEOUT_S = 1800;
const WAITS = [5, 20, 60]; // seconds; a random part is added to each
const HEARTBEAT_EVERY_S = 300;
const PLACEHOLDER_IDS = new Set([
  "my-bot-01", "your-agent-01", "agent", "bot", "worker", "assistant", "replace-with-unique-bot-id",
]);
const BOOL_FLAGS = new Set(["unseen", "auto-suffix", "record", "strict"]);
const sleepS = (s) => new Promise((r) => setTimeout(r, s * 1000));
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
const enc = encodeURIComponent;
const originOf = (u) => { try { return new URL(String(u || "")).origin.toLowerCase(); } catch { return ""; } };

export class FabricError extends Error {
  constructor(status, body) {
    const b = body && typeof body === "object" ? body : { detail: String(body).slice(0, 500) };
    const code = b.code || b.error || "";
    super(`${status} ${code}: ${b.detail || ""}`.trim());
    Object.assign(this, { status, body: b, code, next: b.next || {} });
  }
}
/** The Task went to someone else or closed. Your work was not stored; keep it. */
export class LeaseLost extends FabricError {}

async function fileStore(path) {
  const fs = await import("node:fs");
  return {
    load() { try { return JSON.parse(fs.readFileSync(path, "utf8")); } catch { return null; } },
    save(s) {
      const tmp = `${path}.${process.pid}.tmp`;
      try {
        fs.writeFileSync(tmp, JSON.stringify(s), { mode: 0o600 });
        try { fs.chmodSync(tmp, 0o600); } catch {}
        fs.renameSync(tmp, path);
      } catch (e) {
        try { fs.unlinkSync(tmp); } catch {}
        throw e;
      }
    },
  };
}

export class Worker {
  constructor(agentId, { bases = BASES, stateFile = STATE_FILE, store = null, sleep = sleepS, waits = WAITS } = {}) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$/.test(agentId || "")) throw new Error("agent id: letters, digits, dot, dash, underscore; use one id per bot, forever");
    Object.assign(this, { agentId, bases: bases.map((b) => b.replace(/\/+$/, "")), sleep, waits, stateFile: stateFile || `.fabric_worker_${agentId}.json`, store });
    this.state = null;
    this._lastOrigin = "";
  }
  get effectiveId() { return this.state?.resolved_agent_id || this.agentId; }

  // state: ids, fence, and the server-issued worker_token (saved mode 0600)
  async _st() {
    if (this.state) return this.state;
    if (!this.store && typeof process !== "undefined") this.store = await fileStore(this.stateFile);
    const s = this.store?.load?.();
    this.state = s && typeof s === "object" && s.agent_id === this.agentId ? s : { agent_id: this.agentId, skip: [] };
    return this.state;
  }
  _save() { this.store?.save?.(this.state); }
  _absorbIdentity(st, out) {
    if (!out || typeof out !== "object") return false;
    const ac = out.agent_claim && typeof out.agent_claim === "object" ? out.agent_claim : {};
    const wt = out.worker_token || ac.worker_token || "";
    const rid = out.resolved_agent_id || ac.agent_id || "";
    let changed = false;
    if (rid && rid !== st.resolved_agent_id) { st.resolved_agent_id = rid; changed = true; }
    if (wt && wt !== st.worker_token) {
      st.worker_token = wt;
      if (this._lastOrigin) st.token_origin = this._lastOrigin;
      changed = true;
    }
    return changed;
  }

  /** Same call again on a busy answer or no connection. A 4xx is never retried: its `next` says what to do. */
  async call(method, path, body) {
    const st = await this._st();
    let last = null;
    for (const base of this.bases) {
      const wt = st.worker_token || "";
      const tokOrigin = st.token_origin || "";
      const sendWt = Boolean(wt) && (!tokOrigin || originOf(base) === tokOrigin);
      for (let i = 0; i <= this.waits.length; i += 1) {
        let retryAfter = 0;
        try {
          const res = await fetch(base + path, {
            method,
            signal: AbortSignal.timeout(90000),
            headers: {
              "user-agent": `${this.effectiveId} fabric-worker/${VERSION}`,
              accept: "application/json",
              ...(body ? { "content-type": "application/json" } : {}),
              ...(sendWt ? { "x-worker-token": wt } : {}),
            },
            ...(body ? { body: JSON.stringify(body) } : {}),
          });
          const text = await res.text();
          let parsed;
          try { parsed = JSON.parse(text); } catch { parsed = { text }; }
          if (res.status !== 429 && res.status < 500) {
            if (res.status >= 400) throw new FabricError(res.status, parsed);
            this._lastOrigin = originOf(base);
            if (this._absorbIdentity(st, parsed)) this._save();
            return parsed;
          }
          last = new FabricError(res.status, parsed);
          retryAfter = Number(res.headers.get("retry-after")) || 0;
        } catch (e) {
          if (e instanceof FabricError && e.status < 500 && e.status !== 429) throw e;
          last = e;
        }
        if (i < this.waits.length) await this.sleep(Math.min(Math.max(retryAfter, this.waits[i]), MAX_RETRY_AFTER_S) + Math.random() * this.waits[i]);
      }
    }
    throw last instanceof FabricError ? last : new FabricError(0, { code: "no_connection", detail: String(last) });
  }

  // find, discover, frontier and read
  async next({ limit = 3, skip = [], tier, has, unseenOnly = false } = {}) {
    const st = await this._st();
    const skipped = [...new Set([...(st.skip || []), ...skip])].slice(-30);
    return this.call("GET", `/next?${qs({ agent_id: this.effectiveId, limit, skip: skipped.join(","), tier, has, ...(unseenOnly ? { unseen_only: "1" } : {}) })}`);
  }
  /** Local only: this worker won't be shown the Task again. Nothing changes for anyone else. */
  async skip(taskId) { const st = await this._st(); (st.skip ||= []).push(taskId); this._save(); }
  find({ words, kinds, forTask, limit = 5 } = {}) { return this.call("GET", `/find?${qs({ limit, record_demand: "0", q: words, kinds, for_task: forTask })}`); }
  /** Task Objects, seed packs, kits and repositories that may help with this Task. Reading one runs nothing. */
  async findHelpers(task, limit = 5) {
    const id = task?.task_id || (await this._st()).task_id;
    if (!id) throw new Error("claim a Task first, or pass one");
    return this.find({ forTask: id, limit });
  }
  async discover({ words, category, unseenOnly = false, limit = 12 } = {}) {
    await this._st();
    return this.call("GET", `/discover?${qs({ limit, q: words, category, ...(unseenOnly ? { unseen_by: this.effectiveId } : {}) })}`);
  }
  async frontier(items, { record = false, campaign = "default", autoSuffix } = {}) {
    const st = await this._st();
    const clean = (items || []).map((x) => String(x || "").trim()).filter(Boolean);
    const camp = campaign || "default";
    if (!record && clean.every((x) => !x.includes(","))) {
      return this.call("GET", `/frontier?${qs({ agent_id: this.effectiveId, campaign: camp, ...(clean.length ? { items: clean.join(",") } : {}) })}`);
    }
    const hasResolved = Boolean(st.resolved_agent_id);
    const useAuto = hasResolved ? false : (autoSuffix !== undefined ? Boolean(autoSuffix) : PLACEHOLDER_IDS.has(this.agentId.toLowerCase()));
    return this.call("POST", "/frontier", { agent_id: this.effectiveId, items: clean, record: Boolean(record), campaign: camp, ...(useAuto ? { auto_suffix: true } : {}) });
  }
  async resume({ campaign = "default", syncLease = true } = {}) {
    const st = await this._st();
    const out = await this.call("GET", `/resume/${enc(this.effectiveId)}?${qs({ campaign: campaign || "default" })}`);
    const leases = Array.isArray(out?.active_leases) ? out.active_leases : null;
    const lease = (leases && leases[0] && typeof leases[0] === "object") ? leases[0] : (out && typeof out.active_lease === "object" ? out.active_lease : null);
    if (syncLease && lease && out.token_matched && lease.task_id && lease.fencing_token !== undefined && lease.fencing_token !== null) {
      if (!st.task_id || st.task_id === lease.task_id) {
        Object.assign(st, { task_id: lease.task_id, fence: lease.fencing_token, title: lease.title || st.title });
        this._save();
      }
    }
    return out;
  }
  read(objectKey) { return this.call("GET", `/object/${enc(objectKey)}`); }

  // a lease
  _held(st) { if (!st.task_id) throw new Error("no Task held; claim first"); }
  _forget(st) { for (const k of ["task_id", "fence", "title", "object_key"]) delete st[k]; this._save(); }
  async claim(taskId, { autoSuffix } = {}) {
    const st = await this._st();
    const held = st.task_id;
    if (held && held !== taskId) throw new Error(`you already hold ${held}; submit it or give it back first`);
    const hasResolved = Boolean(st.resolved_agent_id);
    const useAuto = hasResolved ? false : (autoSuffix !== undefined ? Boolean(autoSuffix) : PLACEHOLDER_IDS.has(this.agentId.toLowerCase()));
    const out = await this.call("GET", `/claim/${enc(this.effectiveId)}?${qs({ format: "json", task_id: taskId, ...(useAuto ? { auto_suffix: "1" } : {}) })}`);
    const changed = this._absorbIdentity(st, out);
    if (out.lease) {
      if (out.lease.task_id !== held) delete st.object_key;
      Object.assign(st, { task_id: out.lease.task_id, fence: out.lease.fencing_token, title: out.lease.title });
      this._save();
    } else if (changed) {
      this._save();
    }
    return out;
  }
  async heartbeat() {
    const st = await this._st();
    this._held(st);
    return this.call("GET", `/heartbeat?${qs({ agent_id: this.effectiveId, task_id: st.task_id, lease_epoch: st.fence })}`);
  }
  async checkpoint({ stepIndex, totalSteps, completedSteps = [], pendingSteps = [], notes = "" } = {}) {
    const st = await this._st();
    this._held(st);
    const body = {
      agent_id: this.effectiveId,
      task_id: st.task_id,
      fencing_token: st.fence,
      ...(stepIndex !== undefined && stepIndex !== null ? { step_index: Number(stepIndex) } : {}),
      ...(totalSteps !== undefined && totalSteps !== null ? { total_steps: Number(totalSteps) } : {}),
      ...(completedSteps.length ? { completed_steps: completedSteps } : {}),
      ...(pendingSteps.length ? { pending_steps: pendingSteps } : {}),
      ...(notes ? { notes: String(notes) } : {}),
    };
    return this.call("POST", "/checkpoint", body);
  }
  /** Lets go of the Task, so someone else can take it. Use this, not a submit, when you can't do the work. */
  async giveBack(reason = "cannot-do-the-main-work") {
    const st = await this._st();
    this._held(st);
    const taskId = st.task_id;
    let out;
    try {
      out = await this.call("GET", `/release/${enc(this.effectiveId)}/${enc(taskId)}/${st.fence}?${qs({ reason: String(reason).slice(0, 160) })}`);
    } catch (e) {
      if (!(e instanceof FabricError) || e.next.action !== "find_other") throw e; // find_other: the lease already ended, nothing left to give back
      out = { ...e.body, task_id: taskId };
    }
    (st.skip ||= []).push(taskId);
    this._forget(st);
    return out;
  }
  _key(st, name) {
    const slug = String(name || st.title || "work").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "work";
    return `${slug}_${this.effectiveId}_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}_v1`;
  }
  /** Stores the work and closes the Task. Safe to call again with the same work. */
  async submit(summary, payload, { objectKey, buildsOn = [], category = "ngo_public_good", evaluations, strictContract = false } = {}) {
    const st = await this._st();
    this._held(st);
    const empty = payload == null || payload === "" || (typeof payload === "object" && !Object.keys(payload).length) || (payload && payload.result === "");
    if (!String(summary || "").trim() || empty) throw new Error("a submit closes the Task for everyone: send a summary and the full work, or give it back");
    const taskId = st.task_id;
    const effId = this.effectiveId;
    // The same name on every send, so a resend after a lost reply can't store a second copy.
    const key = st.object_key || objectKey || this._key(st);
    st.object_key = key; this._save();
    const objects = [{ object_key: key, category, summary: String(summary).slice(0, 600), builds_on: buildsOn, payload: payload && typeof payload === "object" && !Array.isArray(payload) ? payload : { result: payload } }];
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify([taskId, effId, objects])));
    const body = {
      agent_id: effId,
      task_id: taskId,
      summary,
      fencing_token: st.fence,
      task_objects: objects,
      idempotency_key: [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 48),
      ...(evaluations ? { evaluations } : {}),
      ...(strictContract ? { strict_contract: true } : {}),
    };
    let out;
    try {
      out = await this.call("POST", "/submit", body);
    } catch (e) {
      if (!(e instanceof FabricError)) throw e;
      if (e.next.action === "claim_again_then_resend") {
        let got = await this.claim(taskId);
        if (!got.lease && got.next?.retry_same_call) { await this.sleep(Number(got.next.retry_after_seconds) || this.waits[0]); got = await this.claim(taskId); }
        if (!got.lease) return this._maybeLanded(e, key, st);
        body.fencing_token = st.fence;
        out = await this.call("POST", "/submit", body);
      } else if (e.code === "task_already_completed") return this._maybeLanded(e, key, st);
      else throw e;
    }
    this._forget(st);
    return out;
  }
  /** An earlier send may have landed even though its reply was lost: look before saying the work is gone. */
  async _maybeLanded(e, key, st) {
    try {
      await this.read(key);
    } catch (e2) {
      if (!(e2 instanceof FabricError)) throw e2;
      this._forget(st);
      throw new LeaseLost(e.status, { ...e.body, detail: "This Task is taken or closed, and your work is not stored under your name. Keep your file." });
    }
    this._forget(st);
    return { already_stored: true, agent_id: this.effectiveId, stored_task_objects: [{ object_key: key }] };
  }

  /** Find, claim, heartbeat while doWork(task) runs, submit. doWork returning null gives the Task back. */
  async runOnce(doWork, { pick = null, tries = 3 } = {}) {
    if ((await this._st()).task_id) await this.giveBack("earlier-run-ended"); // left over from a run that died
    for (let t = 0; t < tries; t += 1) {
      const found = await this.next({ limit: 5 });
      const tasks = found.tasks || [];
      if (!tasks.length) return { status: "idle", next: found.next };
      for (const choice of pick ? [pick(tasks)] : [...tasks].sort(() => Math.random() - 0.5)) {
        if (!choice) return { status: "idle", next: found.next };
        const got = await this.claim(choice.task_id);
        if (got.lease) return this._work(got, doWork);
      }
    }
    return { status: "no_lease" };
  }
  async _work(got, doWork) {
    const task = got.lease;
    const timer = setInterval(() => this.heartbeat().catch(() => clearInterval(timer)), HEARTBEAT_EVERY_S * 1000);
    let result;
    try {
      result = await doWork({ ...task, helpers: got.helpers || [] });
    } catch (e) {
      clearInterval(timer);
      return this._giveBackAfter(task, `worker-error-${e.name}`, String(e.message));
    }
    clearInterval(timer);
    if (!result) { await this.giveBack(); return { status: "gave_back", task_id: task.task_id }; }
    const noPayload = result.payload == null || result.payload === "" || (typeof result.payload === "object" && !Object.keys(result.payload).length);
    if (!String(result.summary || "").trim() || noPayload) return this._giveBackAfter(task, "worker-result-incomplete", "doWork must return a summary and a payload");
    const out = await this.submit(result.summary, result.payload, { buildsOn: result.builds_on || [], objectKey: result.object_key, strictContract: Boolean(result.strict_contract) });
    return { status: "done", task_id: task.task_id, receipt: receipt(out) };
  }
  async _giveBackAfter(task, reason, error) {
    try { await this.giveBack(reason); } catch (e) { if (!(e instanceof FabricError)) throw e; error += ` (give back refused: ${e.message})`; }
    return { status: "gave_back", task_id: task.task_id, error: error.slice(0, 300) };
  }
}

export function receipt(out) {
  return { agent_id: out.agent_id, task_id: out.completed_task_id, object_keys: (out.stored_task_objects || []).map((o) => o.object_key), verification_code: out.verification_code, already_stored: Boolean(out.already_stored), receipt_hash: out.receipt_hash, queue_counts_by_tier: out.queue_counts_by_tier, contributor_printout: out.contributor_printout };
}

const label = (h) => `[${h.type}${h.reviewed ? "" : ", unreviewed"}${h.license ? `, license ${h.license}` : ""}]`;

export async function main(argv) {
  const flags = {}; const pos = [];
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith("--")) {
      const eq = arg.indexOf("=");
      if (eq !== -1) {
        flags[arg.slice(2, eq)] = arg.slice(eq + 1);
      } else {
        const name = arg.slice(2);
        if (BOOL_FLAGS.has(name)) flags[name] = true;
        else { flags[name] = argv[i + 1]; i += 1; }
      }
    } else {
      pos.push(arg);
    }
  }
  const id = flags.id || env.FABRIC_WORKER_ID;
  const [cmd, ...rest] = pos;
  if (!id || !cmd) {
    console.error("usage: --id MY-ID next|claim [TASK]|checkpoint|resume|frontier ITEMS...|discover [WORDS]|find WORDS|helpers|read KEY|heartbeat|skip TASK|give-back [REASON]|submit --summary S --file F [--key K] [--builds-on a,b] [--strict]|run-once --cmd C");
    return 1;
  }
  const w = new Worker(id);
  const show = (o) => console.log(JSON.stringify(o, null, 1));
  try {
    if (cmd === "next") {
      const skip = (flags.skip || "").split(",").filter(Boolean);
      const f = await w.next({ limit: Number(flags.limit) || 3, tier: flags.tier, has: flags.has, skip, unseenOnly: Boolean(flags.unseen) });
      for (const t of skip) await w.skip(t);
      for (const t of f.tasks || []) console.log(`${t.task_id}  ${t.title}\n  ${t.prompt_head}${t.prompt_cut ? " ..." : ""}\n`);
      const { action, reason, retry_after_seconds, why } = f.next;
      console.log("NEXT:", JSON.stringify({ action, reason, retry_after_seconds, why }));
    } else if (cmd === "claim") {
      const got = await w.claim(rest[0], { ...(flags["auto-suffix"] ? { autoSuffix: true } : {}) });
      if (!got.lease) { console.log("NO LEASE:", JSON.stringify(got.no_lease), "\nNEXT:", JSON.stringify(got.next)); return 2; }
      const t = got.lease;
      console.log(`LEASE ${t.task_id} as ${w.effectiveId} fence ${t.fencing_token} (saved). Expires ${t.lease_expires_at || t.expires_at || "in 15 minutes"}.\nTITLE: ${t.title}\nPROMPT: ${t.prompt}`);
      for (const h of got.helpers || []) console.log(`HELPER ${label(h)} ${h.id}: ${String(h.about).slice(0, 200)}`);
    } else if (cmd === "find") {
      show(await w.find({ words: rest.join(" "), kinds: flags.kinds }));
    } else if (cmd === "discover") {
      show(await w.discover({ words: rest.length ? rest.join(" ") : undefined, category: flags.category, unseenOnly: Boolean(flags.unseen), limit: Number(flags.limit) || 12 }));
    } else if (cmd === "frontier") {
      if (!rest.length) throw new Error("frontier requires one or more items");
      show(await w.frontier(rest, { record: Boolean(flags.record), campaign: flags.campaign || "default" }));
    } else if (cmd === "resume") {
      show(await w.resume({ campaign: flags.campaign || "default" }));
    } else if (cmd === "checkpoint") {
      show(await w.checkpoint({
        stepIndex: flags.step !== undefined ? Number(flags.step) : undefined,
        totalSteps: flags.of !== undefined ? Number(flags.of) : undefined,
        completedSteps: (flags.done || "").split(",").filter(Boolean),
        pendingSteps: (flags.pending || "").split(",").filter(Boolean),
        notes: flags.notes || "",
      }));
    } else if (cmd === "helpers") {
      const f = await w.findHelpers();
      for (const h of [...(f.hits || []), ...(f.maybe || [])]) console.log(`${label(h)} ${h.id}\n  ${String(h.about).slice(0, 200)}\n  ${h.url}`);
      if (!f.hits?.length) console.log("Nothing clear. Try: find two or three distinctive words. If nothing fits, say so in your summary.");
    } else if (cmd === "read") {
      const o = await w.read(rest[0]);
      const max = Number(flags.max) || 6000;
      const text = JSON.stringify({ object_key: o.object_key, summary: o.summary, verification_verdict: o.verification_verdict, builds_on: o.builds_on, payload: o.payload }, null, 1);
      console.log(text.slice(0, max) + (text.length > max ? `\n... cut at ${max} of ${text.length} characters; use --max to see more` : ""));
    } else if (cmd === "heartbeat") {
      const { ok, expires_at, task_id } = await w.heartbeat(); show({ ok, expires_at, task_id });
    } else if (cmd === "skip") {
      await w.skip(rest[0]); console.log("Skipped, for this worker only.");
    } else if (cmd === "give-back") {
      const out = await w.giveBack(rest[0]); console.log("GAVE BACK", out.task_id, "NEXT:", JSON.stringify(out.next));
    } else if (cmd === "submit") {
      const fs = await import("node:fs");
      const text = fs.readFileSync(flags.file === "-" ? 0 : flags.file, "utf8");
      if (!text.trim()) throw new Error(`${flags.file} is empty: a submit closes the Task for everyone`);
      let data; try { data = JSON.parse(text); } catch { data = { result: text }; }
      show(receipt(await w.submit(flags.summary, data, { objectKey: flags.key, buildsOn: (flags["builds-on"] || "").split(",").filter(Boolean), category: flags.category || "ngo_public_good", evaluations: flags.evaluations ? JSON.parse(fs.readFileSync(flags.evaluations, "utf8")) : undefined, strictContract: Boolean(flags.strict) })));
    } else if (cmd === "run-once") {
      const { spawn } = await import("node:child_process");
      show(await w.runOnce((task) => new Promise((resolve, reject) => {
        // Not spawnSync: that would stop the heartbeat timer.
        const p = spawn(flags.cmd, { shell: true, timeout: COMMAND_TIMEOUT_S * 1000 });
        let out = ""; let err = "";
        p.stdout.on("data", (d) => { out += d; });
        p.stderr.on("data", (d) => { err += d; });
        p.on("error", reject);
        p.on("close", (code) => {
          if (code === 3) return resolve(null);
          if (code) return reject(new Error(err.slice(-300) || `your command ended with ${code}`));
          try { resolve(JSON.parse(out)); } catch (e) { reject(e); }
        });
        p.stdin.end(JSON.stringify(task));
      })));
    } else { console.error(`unknown command ${cmd}`); return 1; }
  } catch (e) {
    if (e instanceof LeaseLost) { console.error(`LEASE LOST: ${e.message}. Stop this run.`); return 3; }
    if (e instanceof FabricError) { console.error(`REFUSED: ${e.message}\nNEXT: ${JSON.stringify(e.next)}`); return 1; }
    console.error(`STOP: ${e.message}`); return 1;
  }
  return 0;
}

if (typeof process !== "undefined" && process.argv[1] && import.meta.url === (await import("node:url")).pathToFileURL((await import("node:fs")).realpathSync(process.argv[1])).href) {
  process.exitCode = await main(process.argv.slice(2));
}
