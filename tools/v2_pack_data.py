r"""Pack the offline data the V2 tools and data tests need into one zip with a sha256
manifest (Phase 10: move to a safe disk / server without the 5.3 GB pilot DB).

Read-only on the sources. Included (when present under NB_V2_BENCH_DIR):
  v2-phase4-2026-11-12        the 05:12 all-venue snapshot (STRICT vs ESTIMATED, funnel)
  v2-aligned-2026-11-12       the 07:36 aligned burst
  v2-bursts-2026-11-12        the three 12:47-12:53 bursts
  v2-phase8-rescue-2026-11-12 the Phase 8 stream and ledgers rescued from E:
  v2-paper-data/probe-parallel-2026-11-12  the Phase 9 parallel probe
  v2-paper-data/closes-*.json.gz           hourly closes export (tools.v2_export_closes)
Never: SQLite files, anything on D:/E:.

Usage: python -m tools.v2_pack_data --out C:\...\neutralbasis-data-pack.zip
"""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import paths, storage  # noqa: E402

DIRS = ("v2-phase4-2026-11-12", "v2-aligned-2026-11-12", "v2-bursts-2026-11-12",
        "v2-phase8-rescue-2026-11-12", "v2-paper-data/probe-parallel-2026-11-12")


def pack(out):
    bench = paths.bench()
    files = []
    for d in DIRS:
        files += [f for f in (bench / d).rglob("*") if f.is_file()]
    files += sorted((bench / "v2-paper-data").glob("closes-*.json.gz"))
    files = [f for f in files if f.suffix.lower() not in (".sqlite3", ".sqlite", ".db", ".wal")
             and str(f.resolve())[:2].lower() not in ("d:", "e:")]
    storage.check_dir(Path(out).parent)
    man = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            rel = f.relative_to(bench).as_posix()
            b = f.read_bytes()
            man[rel] = dict(bytes=len(b), sha256=hashlib.sha256(b).hexdigest())
            z.writestr("nb-benchmark/" + rel, b)
        z.writestr("nb-benchmark/MANIFEST.json", json.dumps(man, indent=1))
    return dict(files=len(man), bytes=sum(v["bytes"] for v in man.values()),
                zip_bytes=Path(out).stat().st_size)


def verify(zip_path):
    """Re-hash every file in a pack against its MANIFEST.json. Returns dict(status OK /
    FAILED, files, bad [...], missing [...], extra [...]). Read-only."""
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        man = json.loads(z.read("nb-benchmark/MANIFEST.json"))
        bad, missing = [], []
        for rel, meta in sorted(man.items()):
            n = "nb-benchmark/" + rel
            if n not in names:
                missing.append(rel)
                continue
            b = z.read(n)
            if len(b) != meta["bytes"] or hashlib.sha256(b).hexdigest() != meta["sha256"]:
                bad.append(rel)
        extra = sorted(n for n in names - {"nb-benchmark/" + r for r in man}
                       if n != "nb-benchmark/MANIFEST.json")
    return dict(status="OK" if not (bad or missing or extra) else "FAILED",
                files=len(man), bad=bad, missing=missing, extra=extra,
                note="a working-data pack, NOT a backup of the 29.6 GB engine database "
                     "(paper.sqlite3 26.5 GB + WAL 3.1 GB on the failing E:)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--verify")
    a = ap.parse_args()
    if a.verify:
        r = verify(a.verify)
        print(json.dumps(r, indent=1))
        raise SystemExit(0 if r["status"] == "OK" else 1)
    if not a.out:
        raise SystemExit("--out or --verify is required")
    print(json.dumps(pack(a.out), indent=1))


if __name__ == "__main__":
    main()
