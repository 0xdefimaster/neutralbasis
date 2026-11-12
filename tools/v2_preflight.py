"""Pre-migration check of databases and backups. PAPER / research only; READ-ONLY.

Before any database is moved, migrated or reopened by a new engine run, this records
for each SQLite file: size, WAL size, mtime, a hash of its schema (sqlite_master),
row counts per table and SQLite's own `pragma quick_check`, all through a read-only
connection (URI mode=ro). It also checks the git side: each bundle with
`git bundle verify`, and the local branches and their commits.

Files on a disk the owner marked as failing can be listed with --stat-only: size and
mtime are read from the directory, the file itself is never opened (a full read of a
26 GB file on a disk with bad blocks is itself a risk).

Usage:
  python -m tools.v2_preflight --out FILE.json --db PATH [--db PATH ...]
         [--stat-only PATH ...] [--bundle PATH ...] [--repo DIR]
"""
import argparse
import datetime
import hashlib
import json
import sqlite3
import subprocess
import time
from pathlib import Path


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % Path(path).as_posix(), uri=True, timeout=30)


def stat(path):
    p = Path(path)
    if not p.exists():
        return dict(path=str(p), exists=False)
    st = p.stat()
    out = dict(path=str(p), exists=True, size=st.st_size,
               mtime=datetime.datetime.fromtimestamp(st.st_mtime).isoformat())
    for suffix in ("-wal", "-shm"):
        x = Path(str(p) + suffix)
        out[suffix[1:] + "_size"] = x.stat().st_size if x.exists() else None
    return out


def check_db(path, quick=True):
    out = stat(path)
    if not out["exists"]:
        return out
    db = ro(path)
    try:
        schema = db.execute(
            "select type, name, sql from sqlite_master order by type, name").fetchall()
        out["schema_sha256"] = hashlib.sha256(
            json.dumps(schema).encode()).hexdigest()
        out["tables"] = {}
        for (name,) in db.execute(
                "select name from sqlite_master where type='table' order by name"):
            out["tables"][name] = db.execute(
                'select count(*) from "%s"' % name.replace('"', '""')).fetchone()[0]
        out["journal_mode"] = db.execute("pragma journal_mode").fetchone()[0]
        if quick:
            t = time.time()
            res = [r[0] for r in db.execute("pragma quick_check")]
            out["quick_check"] = res[:20]
            out["quick_check_ok"] = res == ["ok"]
            out["quick_check_seconds"] = round(time.time() - t, 1)
    finally:
        db.close()
    return out


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo)] + list(args), capture_output=True,
                       text=True, timeout=300)
    return dict(ok=r.returncode == 0, out=(r.stdout or r.stderr).strip()[-2000:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", action="append", default=[])
    ap.add_argument("--stat-only", action="append", default=[])
    ap.add_argument("--bundle", action="append", default=[])
    ap.add_argument("--repo")
    ap.add_argument("--no-quick-check", action="store_true")
    a = ap.parse_args()
    report = dict(
        created=datetime.datetime.now().isoformat(),
        mode="READ-ONLY",
        databases=[check_db(p, quick=not a.no_quick_check) for p in a.db],
        stat_only=[stat(p) for p in a.stat_only])
    if a.repo:
        report["git"] = dict(
            head=git(a.repo, "rev-parse", "--abbrev-ref", "HEAD"),
            branches=git(a.repo, "branch", "-v", "--no-abbrev"),
            status=git(a.repo, "status", "--short"),
            bundles={b: git(a.repo, "bundle", "verify", b) for b in a.bundle})
    Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    ok = all(d.get("quick_check_ok", True) for d in report["databases"] if d["exists"])
    print("wrote", a.out, "quick_check all ok" if ok else "QUICK_CHECK FAILED")


if __name__ == "__main__":
    main()
