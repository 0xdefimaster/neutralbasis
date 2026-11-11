"""Isolated PAPER pilot: the worktree engine on its own port and its own C: database.

PAPER ONLY. No account, no keys, no orders. Before any engine module is imported, the
process installs pilot.network_guard (public market-data reads only; trading/account
paths, unknown hosts and the live engine's port are refused and logged). Scanning is
limited to the perp DEX venues (capabilities.PERP_DEXES); reference CEX adapters are
switched off in this process only (no engine file changes).

Environment: NB_PILOT_DATA (default C:\\Users\\W10\\nb-pilot\\data), NB_PILOT_PORT
(default 18765), NB_PILOT_MAX_GB (default 5: above it new cycles stop).
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pilot import network_guard  # noqa: E402

DATA = Path(os.environ.get("NB_PILOT_DATA", r"C:\Users\W10\nb-pilot\data")).resolve()
PORT = int(os.environ.get("NB_PILOT_PORT", "18765"))
MAX_GB = float(os.environ.get("NB_PILOT_MAX_GB", "5"))
# Raw API response bodies kept in this pilot's audit table, in days (the engine's own
# default is 3). The body is ~all of a pilot database (~2.6 GB/day measured
# 2026-11-11); after this window only the fingerprint (time, URL, sha256) stays, and
# bodies referenced by any position, fill or payment are always kept (retention.py).
AUDIT_RAW_DAYS = float(os.environ.get("NB_PILOT_AUDIT_RAW_DAYS", "1"))
LIVE_DATA = Path(r"E:\NeutralBasis").resolve()


def refuse(msg):
    print("PILOT REFUSED:", msg, flush=True)
    raise SystemExit(2)


def folder_bytes(path):
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def main():
    if str(DATA).lower().startswith("e:") or LIVE_DATA in DATA.parents or DATA == LIVE_DATA:
        refuse("data folder must not be on E: or inside the live project: %s" % DATA)
    if PORT == network_guard.LIVE_ENGINE_PORT:
        refuse("port %d belongs to the live engine" % PORT)
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    guard = network_guard.install(str(DATA / "logs" / "network-guard.jsonl"))

    import capabilities
    import wide_markets

    keep = set(capabilities.PERP_DEXES)
    off = []
    for v, spec in wide_markets.CATALOG.items():
        if v not in keep and (spec.get("adapter") or spec.get("scan")):
            spec["adapter"], spec["scan"] = None, False
            off.append(v)

    import bot
    import retention
    import risk_main

    if not 0.25 <= AUDIT_RAW_DAYS <= retention.AUDIT_KEEP_DAYS:
        refuse("NB_PILOT_AUDIT_RAW_DAYS out of range: %s" % AUDIT_RAW_DAYS)
    retention.AUDIT_RAW_DAYS = AUDIT_RAW_DAYS
    engine = risk_main.RiskEngine(DATA / "paper.sqlite3")
    engine.running = True
    crash = risk_main.enable_crash_log(DATA / "logs")
    started = time.time()
    info = dict(pid=os.getpid(), started=started, port=PORT, data=str(DATA), mode="PAPER",
                code=str(ROOT), scan_venues=sorted(keep), reference_off=sorted(off),
                crash_log=str(crash), audit_raw_days=AUDIT_RAW_DAYS)
    (DATA / "pilot.json").write_text(json.dumps(info, indent=1), encoding="utf8")
    print("PAPER pilot", json.dumps(info), flush=True)

    def watchdog():
        while True:
            time.sleep(300)
            size = folder_bytes(DATA)
            state = dict(t=time.time(), data_bytes=size, guard=dict(guard.stats))
            if size > MAX_GB * 1e9 and engine.running:
                engine.running = False
                state["stopped"] = "disk limit %.1f GB reached; new cycles paused" % MAX_GB
                (DATA / "STOPPED_DISK_LIMIT").write_text(json.dumps(state), encoding="utf8")
            with open(DATA / "logs" / "watchdog.jsonl", "a", encoding="utf8") as f:
                f.write(json.dumps(state) + "\n")

    threading.Thread(target=watchdog, daemon=True, name="pilot-watchdog").start()
    bot.serve(engine, PORT, "127.0.0.1", None)


if __name__ == "__main__":
    main()
