"""Offline V2 dashboard: one Runtime round on recorded phase 4 answers -> HTML.

PAPER / research; no network, no engine. Replays tools/v2_phase4_eval's answers
through the real V2 runtime (collector -> candidates -> gate -> hybrid desk in a temp
folder, hybrid opening off) and writes the dashboard page and the snapshot JSON.

Usage: python -m tools.v2_dashboard --phase4 DIR --out FILE.html [--assume-session-open]
"""
import argparse
import gzip
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wide_paper  # noqa: E402
from tools.v2_phase4_eval import PILOT_DB, Provider, Replay, pilot_closes  # noqa: E402
from v2 import dashboard, gates  # noqa: E402
from v2.collector import Collector  # noqa: E402
from v2.hybrid_desk import HybridDesk  # noqa: E402
from v2.runtime import Runtime  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase4", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--assume-session-open", action="store_true")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if str(out).lower().startswith("e:"):
        raise SystemExit("must not write on E:")
    blob = json.loads(gzip.decompress((Path(a.phase4) / "answers.json.gz").read_bytes()))
    at = blob["at"]
    col = Collector(transport=Replay(blob["answers"]), clock=lambda: at)
    closes, _ = pilot_closes(PILOT_DB)
    prov = Provider(col, closes, a.assume_session_open)
    cfg = dict(wide_paper.WIDE_DEFAULTS)
    gate = gates.Gate(prov, cfg)
    with tempfile.TemporaryDirectory() as d:
        desk = HybridDesk(Path(d) / "h.json", prov, cfg, clock=lambda: at)
        rt = Runtime(col, gate, cfg, desk=desk, clock=lambda: at)
        rt.evaluate_once()
        snap = rt.snapshot()
    out.write_text(dashboard.render(snap), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(snap, indent=1, default=str),
                                        encoding="utf-8")
    for s, x in snap["strategies"].items():
        print(s, x["candidates"], x["by_category"])
    print("wrote", out)


if __name__ == "__main__":
    main()
