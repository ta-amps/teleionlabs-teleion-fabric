#!/usr/bin/env python3
"""driver.py ROUND --prefix opencode-muse- --model meta/muse-spark-1.3-contributor --bots N --minutes M --every S [--start 1]
Runs real OpenCode bots on spread schedules, each run in a new clean folder, PROMPT.md's main block as the only instruction.
Starts no new run if STOP exists, if the shared Meta day passes $20, or after a bot has 3 bad runs in a row. A run gets 20 minutes at most."""
import argparse, datetime, json, os, pathlib, re, shutil, subprocess, threading, time
D = pathlib.Path(__file__).resolve().parent
MAIN = re.findall(r'```text\n(.*?)```', (D.parent/'PROMPT.md').read_text(), re.S)[0]
ap = argparse.ArgumentParser(); ap.add_argument('round'); ap.add_argument('--prefix', required=True); ap.add_argument('--model', required=True)
ap.add_argument('--bots', type=int, default=1); ap.add_argument('--minutes', type=float, default=0); ap.add_argument('--every', type=float, default=600); ap.add_argument('--start', type=int, default=1); ap.add_argument('--cap', type=int, default=1200)
ap.add_argument('--once', action='store_true'); ap.add_argument('--harness', default='opencode'); ap.add_argument('--short', action='store_true'); ap.add_argument('--port', default='8911')
A = ap.parse_args()
SHARED = pathlib.Path.home()/'.local/state/eranos-worker/meta-spend.json'
lock = threading.Lock()
def day():
    try: return float(json.loads(SHARED.read_text())['days'].get(time.strftime('%Y-%m-%d', time.gmtime()), 0))
    except Exception: return 0.0
def rec(r):
    with lock:
        with open(D/'logs/runs.jsonl', 'a') as f: f.write(json.dumps(r)+'\n')
def stop(): return (D/'STOP').exists() or day() >= float(os.environ.get('STOP_DAY_USD', 20))
def cfg(d, bot):
    prov, model = A.model.split('/', 1)
    (d/'.home').mkdir(parents=True, exist_ok=True)
    if A.harness == 'codex':
        (d/'.home/.codex').mkdir()
        (d/'.home/.codex/config.toml').write_text(f'model = "{model}"\nmodel_provider = "metaproxy"\n[model_providers.metaproxy]\nname = "Meta through local proxy"\nbase_url = "http://127.0.0.1:{A.port}/b/{bot}/{A.round}/meta/v1"\nwire_api = "responses"\n')
        return
    (d/'opencode.json').write_text(json.dumps({"provider": {prov: {"npm": "@ai-sdk/openai-compatible", "name": prov, "options": {"baseURL": f"http://127.0.0.1:8911/b/{bot}/{A.round}/{prov}/v1", "apiKey": "held-by-proxy"}, "models": {model: {"name": model, "tool_call": True}}}}}))
def run_once(bot, k):
    d = D/'clean'/A.round/f'{bot}-{k}'; shutil.rmtree(d, ignore_errors=True); cfg(d, bot)
    prompt = (re.findall(r'```text\n(.*?)```', (D.parent/'PROMPT.md').read_text(), re.S)[1] if A.short else MAIN).replace('YOUR ID: worker-01', f'YOUR ID: {bot}', 1); assert bot in prompt
    t = D/'transcripts'/f'{A.round}-{bot}-{k}.jsonl'; t0 = time.time(); ts = datetime.datetime.now(datetime.UTC).isoformat(); code = None
    with open(t, 'w') as o, open(str(t)+'.err', 'w') as e:
        cmd = ['opencode.exe', 'run', '--standalone', '--auto', '--format', 'json', '-m', A.model, prompt] if A.harness == 'opencode' else ['codex', 'exec', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox', '--json', prompt]
        p = subprocess.Popen([str(D/'sandbox.sh'), str(d)] + cmd, stdin=subprocess.DEVNULL, stdout=o, stderr=e, start_new_session=True)
        try: code = p.wait(A.cap)
        except subprocess.TimeoutExpired: code = 'timeout'; os.killpg(p.pid, 9); p.wait()
    log = d/'.home/curl.log'
    if log.exists(): shutil.copy(log, D/'logs'/f'curl-{A.round}-{bot}-{k}.log')
    txt = t.read_text(errors='replace'); sub = len(re.findall(r'/q/submit', txt))
    rec({'ts': ts, 'round': A.round, 'bot': bot, 'run': k, 'exit': code, 's': round(time.time()-t0, 1), 'bytes': len(txt)})
    shutil.rmtree(d/'.home/.cache', ignore_errors=True)
    return code
def bot_loop(i):
    bot = f'{A.prefix}{i:02d}'; end = time.time() + A.minutes*60
    time.sleep((i - A.start) * A.every / A.bots)  # spread over one period
    k, bad = 0, 0; 
    while True:
        if stop(): rec({'round': A.round, 'bot': bot, 'event': 'stop_seen'}); return
        k += 1; t0 = time.time(); c = run_once(bot, k); bad = bad+1 if c != 0 else 0
        if bad >= 3: rec({'round': A.round, 'bot': bot, 'event': 'three_bad'}); return
        if A.once or time.time() + 60 > end: return
        time.sleep(max(5, A.every - (time.time()-t0)))
ts = [threading.Thread(target=bot_loop, args=(i,)) for i in range(A.start, A.start+A.bots)]
[t.start() for t in ts]; [t.join() for t in ts]
print('done', A.round, flush=True)
