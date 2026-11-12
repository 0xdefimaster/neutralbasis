"""Machine-specific data locations (Phase 10: portable setup).

Every offline tool and data test resolves its inputs here, so another machine or a
server only sets environment variables (see deploy/README.md); nothing else changes.

  NB_V2_BENCH_DIR     observation snapshots, bursts, streams, rescued Phase 8 data
                      (default C:\\Users\\W10\\nb-benchmark)
  NB_V2_PILOT_DB      the loose pilot's SQLite (hourly closes; opened READ-ONLY)
                      (default C:\\Users\\W10\\nb-pilot-loose\\data\\paper.sqlite3)
  NB_V2_CLOSES_FILE   a closes export from tools.v2_export_closes (json.gz); used
                      instead of the 5.3 GB pilot DB when set and present
  NB_V2_PAPER_DATA_DIR  where ESTIMATED PAPER outputs go (v2.storage)
"""
import gzip
import json
import os
from pathlib import Path

DEFAULT_BENCH = r"C:\Users\W10\nb-benchmark"
DEFAULT_PILOT_DB = r"C:\Users\W10\nb-pilot-loose\data\paper.sqlite3"


def bench(*parts, environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get("NB_V2_BENCH_DIR") or DEFAULT_BENCH, *parts)


def pilot_db(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get("NB_V2_PILOT_DB") or DEFAULT_PILOT_DB)


def closes_file(environ=None):
    env = os.environ if environ is None else environ
    v = env.get("NB_V2_CLOSES_FILE")
    return Path(v) if v else None


def closes_available(environ=None):
    f = closes_file(environ)
    return bool((f and f.exists()) or pilot_db(environ).exists())


def load_closes(environ=None):
    """{(venue, asset): {hour ts: close}} from the export when present, else from the
    pilot DB (read-only). Raises FileNotFoundError when neither exists (never empty
    data presented as history)."""
    f = closes_file(environ)
    if f and f.exists():
        blob = json.loads(gzip.decompress(f.read_bytes()))
        return {tuple(k.split("|", 1)): {int(t): p for t, p in v.items()}
                for k, v in blob["closes"].items()}
    db = pilot_db(environ)
    if not db.exists():
        raise FileNotFoundError("no hourly closes: set NB_V2_CLOSES_FILE or NB_V2_PILOT_DB")
    from tools.v2_phase4_eval import pilot_closes

    return dict(pilot_closes(str(db))[0])
