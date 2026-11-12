"""Long-running safety checks: storage location, free disk, SQLite WAL growth.

PAPER / read-only. None of these checks writes to a database or checkpoints a WAL;
they report, and the caller stops new work (fail-closed) when a limit is crossed.
"""
import os
import shutil
import time
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


def atomic_write(path, text, tries=20, wait=0.05, replace=os.replace, sleep=time.sleep):
    """Write `text` to `path` through a temp file and an atomic replace. On Windows the
    replace fails transiently while another process (antivirus, indexer) holds the
    target (WinError 5); it is retried. If every try fails the temp file is kept and
    the error raised: the previous state file is never left half written."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    return replace_retry(tmp, path, tries, wait, replace, sleep)


def replace_retry(src, dst, tries=20, wait=0.05, replace=os.replace, sleep=time.sleep):
    """os.replace retried on transient Windows locks; returns the retries used."""
    for i in range(tries):
        try:
            replace(src, dst)
            return i
        except PermissionError:
            if i == tries - 1:
                raise
            sleep(wait * (i + 1))


# The TOSHIBA HDD that holds D: and E: logs bad blocks (System log event 7); nothing of
# V2 may run there (docs/v2/14_OPERATION_BACKUP_RUNBOOK.md).
FAILING_DRIVES = ("D:", "E:")
LAUNCH_MIN_FREE = 50 * 1024 ** 3
BACKUP_MAX_AGE_DAYS = 7


def launch_check(data_dir, backup_evidence=None, min_free=LAUNCH_MIN_FREE,
                 disk_usage=shutil.disk_usage, clock=time.time):
    """Before a live PAPER pilot: a healthy disk, room for it and proof of a backup.
    Returns (ok, findings); every finding blocks the start.

    backup_evidence: a JSON file (default <data_dir>/BACKUP_EVIDENCE.json) written when
    a backup was made and checked, with `created` (epoch s), `destination` (a path on a
    different, healthy drive) and `sha256_manifest` (non-empty)."""
    import json

    findings = []
    p = Path(data_dir)
    drive = (p.drive or str(p)[:2]).upper()
    if drive in FAILING_DRIVES:
        return False, ["%s is on the failing TOSHIBA disk (bad blocks): refused" % drive]
    try:
        free = disk_usage(str(p if p.exists() else p.parent)).free
    except OSError as ex:
        return False, ["data folder unreadable: %s" % ex]
    if free < min_free:
        findings.append("disk free %.1f GB < %.1f GB required for a 24/7 pilot"
                        % (free / 1024 ** 3, min_free / 1024 ** 3))
    ev = Path(backup_evidence) if backup_evidence else p / "BACKUP_EVIDENCE.json"
    try:
        b = json.loads(ev.read_text(encoding="utf-8"))
        dest = str(b.get("destination") or "")
        if clock() - float(b.get("created", 0)) > BACKUP_MAX_AGE_DAYS * 86400:
            findings.append("backup evidence older than %d days" % BACKUP_MAX_AGE_DAYS)
        if not b.get("sha256_manifest"):
            findings.append("backup evidence has no sha256 manifest")
        if dest[:2].upper() in FAILING_DRIVES or dest[:2].upper() == drive:
            findings.append("backup destination %s is not a separate healthy drive" % dest)
    except (OSError, ValueError):
        findings.append("no backup evidence (%s)" % ev)
    return not findings, findings
