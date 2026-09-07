"""Restart mechanism. Runs the bot as a child process, restarts it on crash/exit or when the heartbeat file goes stale.
Windows: run `py supervisor.py` from Task Scheduler (At log on, restart on failure) or as an NSSM service."""
import json, os, subprocess, sys, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HEARTBEAT = BASE_DIR / "data" / "heartbeat.json"
STALE_SECONDS = 180          # 3 × telemetry flush interval
BACKOFF = [5, 15, 30, 60, 120]
CMD = [sys.executable, "-m", "xau_mt5_bot.main", "--config", str(BASE_DIR / "config.yaml"), "--loop"]

def heartbeat_age():
    try: return time.time() - json.loads(HEARTBEAT.read_text())["ts_epoch"]
    except Exception:
        try: return time.time() - HEARTBEAT.stat().st_mtime
        except Exception: return None

def main():
    env = {**os.environ, "PYTHONPATH": str(BASE_DIR / "src")}
    attempt = 0
    while True:
        start = time.time()
        proc = subprocess.Popen(CMD, env=env, cwd=BASE_DIR)
        print(f"[supervisor] started pid {proc.pid}", flush=True)
        while proc.poll() is None:
            time.sleep(10)
            age = heartbeat_age()
            if age is not None and age > STALE_SECONDS and time.time() - start > STALE_SECONDS:
                print(f"[supervisor] heartbeat stale {age:.0f}s — killing", flush=True); proc.kill(); break
        code = proc.wait()
        attempt = 0 if time.time() - start > 600 else attempt + 1      # ran >10 min → reset backoff
        delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
        print(f"[supervisor] exit {code}, restart in {delay}s", flush=True)
        time.sleep(delay)

if __name__ == "__main__":
    main()
