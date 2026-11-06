#!/usr/bin/env python3
"""kit_driver.py ROUND --prompt FILE --bots N --prefix opencode-kit-a- : real OpenCode bots on Muse Spark, one run each, from clean folders, through the swarm's sandbox and recording proxy.
Same limits as the swarm driver: stops at the shared Meta day stop; every model attempt is on the ledgers via the proxy. The prompt file's first ```text block is the bot's only instruction."""
import argparse, datetime, json, os, pathlib, re, shutil, subprocess, threading, time
S = pathlib.Path(__import__('os').environ['TELEION_NOTES']) / 'work-2026-11-03/agent-prompt/swarm'
SHARED = pathlib.Path.home()/'.local/state/eranos-worker/meta-spend.json'
ap = argparse.ArgumentParser(); ap.add_argument('round'); ap.add_argument('--prompt', required=True); ap.add_argument('--prefix', required=True); ap.add_argument('--bots', type=int, default=6); ap.add_argument('--stop-day', type=float, default=19.9); ap.add_argument('--spread', type=float, default=20); ap.add_argument('--start', type=int, default=1); ap.add_argument('--port', default='8911')
ap.add_argument('--model', default='meta/muse-spark-1.3-contributor'); ap.add_argument('--cap', type=int, default=900); A = ap.parse_args()
MAIN = re.findall(r'```text\n(.*?)```', pathlib.Path(A.prompt).read_text(), re.S)[0]
def day():
    try: return float(json.loads(SHARED.read_text())['days'].get(time.strftime('%Y-%m-%d', time.gmtime()), 0))
    except Exception: return 0.0
def run(i):
    time.sleep((i - A.start) * A.spread)
    bot = f'{A.prefix}{i:02d}'
    if day() >= A.stop_day:
        print('skip', bot, 'day', day(), flush=True); return
    d = S/'clean'/A.round/f'{bot}-1'; shutil.rmtree(d, ignore_errors=True); (d/'.home').mkdir(parents=True)
    prov, model = A.model.split('/', 1)
    (d/'opencode.json').write_text(json.dumps({"provider": {prov: {"npm": "@ai-sdk/openai-compatible", "name": prov, "options": {"baseURL": f"http://127.0.0.1:{A.port}/b/{bot}/{A.round}/{prov}/v1", "apiKey": "held-by-proxy"}, "models": {model: {"name": model, "tool_call": True}}}}}))
    prompt = MAIN.replace('YOUR ID: worker-01', f'YOUR ID: {bot}', 1); assert bot in prompt
    t = S/'transcripts'/f'{A.round}-{bot}-1.jsonl'; t0 = time.time(); ts = datetime.datetime.now(datetime.UTC).isoformat()
    with open(t, 'w') as o, open(str(t)+'.err', 'w') as e:
        p = subprocess.Popen([str(S/'sandbox.sh'), str(d), 'opencode.exe', 'run', '--standalone', '--auto', '--format', 'json', '-m', A.model, prompt], stdin=subprocess.DEVNULL, stdout=o, stderr=e, start_new_session=True)
        try: code = p.wait(A.cap)
        except subprocess.TimeoutExpired: code = 'timeout'; os.killpg(p.pid, 9); p.wait()
    log = d/'.home/curl.log'
    if log.exists(): shutil.copy(log, S/'logs'/f'curl-{A.round}-{bot}-1.log')
    with open(S/'logs/runs.jsonl', 'a') as f: f.write(json.dumps({'ts': ts, 'round': A.round, 'bot': bot, 'run': 1, 'exit': code, 's': round(time.time()-t0, 1), 'bytes': t.stat().st_size})+'\n')
    shutil.rmtree(d/'.home/.cache', ignore_errors=True); print('done', bot, code, flush=True)
ts = [threading.Thread(target=run, args=(i,)) for i in range(A.start, A.start+A.bots)]
[t.start() for t in ts]; [t.join() for t in ts]
