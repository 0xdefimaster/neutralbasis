r"""Export the hourly closes the V2 strategies need from the loose pilot's SQLite into a
small json.gz (Phase 10: portable setup, no 5.3 GB DB to move).

Read-only on the source (sqlite URI mode=ro). Output: {"closes": {"venue|asset":
{hour ts: close}}, "source": ..., "end": ..., "sha256_of_closes": ...}. Point
NB_V2_CLOSES_FILE at the file on the other machine (v2.paths.load_closes).

Usage: python -m tools.v2_export_closes --out C:\...\closes-YYYY-MM-DD.json.gz
"""
import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.v2_phase4_eval import pilot_closes  # noqa: E402
from v2 import paths, storage  # noqa: E402


def export(db, out):
    closes, end = pilot_closes(str(db))
    body = {"%s|%s" % k: {str(int(t)): p for t, p in sorted(v.items())}
            for k, v in sorted(closes.items())}
    raw = json.dumps(body, sort_keys=True).encode()
    blob = dict(closes=body, source=str(db), end=end, series=len(body),
                points=sum(len(v) for v in body.values()),
                sha256_of_closes=hashlib.sha256(raw).hexdigest(),
                label="OBSERVED hourly closes from the loose pilot (hist_candles, kind=perp)")
    storage.check_dir(Path(out).parent)
    Path(out).write_bytes(gzip.compress(json.dumps(blob).encode()))
    return {k: v for k, v in blob.items() if k != "closes"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(paths.pilot_db()))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(json.dumps(export(a.db, a.out), indent=1))


if __name__ == "__main__":
    main()
