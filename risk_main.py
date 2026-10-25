import argparse, json
from pathlib import Path
import bot
from desk_analytics import DeskEngine as RiskEngine

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default=str(bot.ROOT / "data-five-rh"))
    p.add_argument("--autostart", action="store_true")
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    d = Path(a.data)
    d.mkdir(parents=True, exist_ok=True)
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
        bot.serve(engine, a.port)
