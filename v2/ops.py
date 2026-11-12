"""Long-running safety checks: storage location, free disk, SQLite WAL growth.

PAPER / read-only. None of these checks writes to a database or checkpoints a WAL;
they report, and the caller stops new work (fail-closed) when a limit is crossed.
"""
import os
import shutil
from pathlib import Path

WAL_WARN = 1 * 1024 ** 3      # 1 GB: report
WAL_STOP = 3 * 1024 ** 3      # 3 GB (the 2026-11 E: engine WAL size): stop new work
DISK_MIN_FREE = 5 * 1024 ** 3


def not_on_e(path):
    p = Path(path).resolve()
    if str(p).lower().startswith("e:"):
        raise ValueError("V2 data must not be on E: (failing disk, no backup): %s" % p)
    return p


def wal_status(db_path, warn=WAL_WARN, stop=WAL_STOP):
    wal = Path(str(db_path) + "-wal")
    size = wal.stat().st_size if wal.exists() else 0
    level = "STOP" if size >= stop else "WARN" if size >= warn else "OK"
    return dict(wal=str(wal), bytes=size, level=level)


def disk_status(path, min_free=DISK_MIN_FREE, disk_usage=shutil.disk_usage):
    u = disk_usage(str(path))
    return dict(path=str(path), free=u.free, total=u.total,
                level="STOP" if u.free < min_free else "OK")


def preflight(data_dir, db_path=None, min_free=DISK_MIN_FREE, disk_usage=shutil.disk_usage):
    """Checks before a V2 PAPER run starts; returns (ok, findings)."""
    findings = []
    try:
        not_on_e(data_dir)
    except ValueError as ex:
        return False, [str(ex)]
    d = disk_status(data_dir, min_free, disk_usage)
    if d["level"] != "OK":
        findings.append("disk free %d < %d" % (d["free"], min_free))
    if db_path and os.path.exists(db_path):
        w = wal_status(db_path)
        if w["level"] == "STOP":
            findings.append("WAL %d bytes >= stop limit" % w["bytes"])
    return not findings, findings
