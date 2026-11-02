"""python -m marketdata [--root phase1-evidence] [--instruments] [--json PATH]"""

import argparse
import json
import sys

from .report import build, text


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="python -m marketdata")
    p.add_argument("--root", default="phase1-evidence", help="Kaydedici klasörü")
    p.add_argument(
        "--instruments", action="store_true", help="Tüm enstrümanları listele"
    )
    p.add_argument("--json", help="Raporu bu JSON dosyasına da yaz")
    a = p.parse_args(argv)
    rep = build(a.root)
    print(text(rep, a.instruments))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, ensure_ascii=False)
        print("JSON: %s" % a.json)
    return 1 if rep["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
