import argparse, faulthandler, json, os, sys, time
from pathlib import Path
import bot, retention
from desk_analytics import DeskEngine as RiskEngine


def keep_awake():
    """On Windows, keep the machine from sleeping while the desk runs: a sleeping desk
    stops watching its positions and the paper period gets a gap. The screen may still
    turn off. Elsewhere this does nothing."""
    if os.name != "nt":
        return False
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    # The return value is the previous state, which may be zero; it is not a result.
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    return True


# Kept open for the life of the process: faulthandler writes to this file descriptor
# from its fatal-error handler, so it must never be closed or garbage-collected.
CRASH_LOG = None


def enable_crash_log(folder, at=None, pid=None):
    """Send faulthandler's fatal-error stacks (all threads) to a new file per run, so a
    native crash leaves Python stacks on disk however the engine was started (hidden
    by Baslat.ps1 or in a terminal). Never overwrites an earlier run's file."""
    global CRASH_LOG
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(at))
    path = folder / f"faulthandler-{stamp}-{pid or os.getpid()}.log"
    f = open(path, "a", encoding="utf8")
    f.write(
        f"run started {time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(at))}"
        f" pid={pid or os.getpid()} python={sys.version.split()[0]} mode=PAPER\n"
    )
    f.flush()
    faulthandler.enable(file=f, all_threads=True)
    CRASH_LOG = f
    import runtime_metrics

    # Cycle stall dumps go to the same per-run file.
    runtime_metrics.STALL_LOG = f
    return path


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    # Hosted deploys (Railway) configure through env; local defaults are unchanged.
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default=os.environ.get("NB_HOST", "127.0.0.1"))
    p.add_argument(
        "--data",
        default=os.environ.get("NB_DATA_DIR")
        or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        or str(bot.ROOT / "data-five-rh"),
    )
    p.add_argument("--autostart", action="store_true")
    p.add_argument("--once", action="store_true")
    # One-time full VACUUM and pruning, done while nothing else holds the database.
    p.add_argument("--maintain", action="store_true")
    a = p.parse_args()
    d = Path(a.data)
    d.mkdir(parents=True, exist_ok=True)
    if a.maintain:
        retention.maintain(d / "paper.sqlite3")
        raise SystemExit(0)
    engine = RiskEngine(d / "paper.sqlite3")
    engine.running = a.autostart
    if a.once:
        engine.cycle()
        (d / "last-scan.json").write_text(
            json.dumps(engine.snapshot(), ensure_ascii=False, allow_nan=False),
            encoding="utf8",
        )
        print("Risk taraması kaydedildi:", d / "last-scan.json")
    else:
        print("Çökme yığını kaydı:", enable_crash_log(d / "logs"))
        if keep_awake():
            print("Uyku modu motor çalıştığı sürece engellendi.")
        bot.serve(
            engine, a.port, a.host, os.environ.get("NB_DASHBOARD_PASSWORD") or None
        )
