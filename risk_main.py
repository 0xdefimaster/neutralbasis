import argparse, json, os
from pathlib import Path
import bot, retention
from desk_analytics import DeskEngine as RiskEngine

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
        bot.serve(
            engine, a.port, a.host, os.environ.get("NB_DASHBOARD_PASSWORD") or None
        )
