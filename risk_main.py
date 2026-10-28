import argparse, json, os
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
    return bool(
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
    )


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
    retention.maintain(d / "paper.sqlite3")
    if a.maintain:
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
        if keep_awake():
            print("Uyku modu motor çalıştığı sürece engellendi.")
        bot.serve(
            engine, a.port, a.host, os.environ.get("NB_DASHBOARD_PASSWORD") or None
        )
