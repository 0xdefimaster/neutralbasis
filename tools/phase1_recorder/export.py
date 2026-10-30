"""One zip of everything recorded, for review elsewhere.

    phase1-evidence-export/
      manifest.json            git commit, recorder version, capture window, venues,
                               markets, endpoints, counts, file hashes, warnings
      source_versions.json
      coverage_report.json
      consistency_report.json
      audit.jsonl              every request (public URLs, status, timing, hash)
      raw/  normalized/  errors/
      checksums.sha256

Only relative paths are written; no local user folder, account or key is included.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import zipfile

from . import VERSION
from .storage import iso
from .validation import write_reports

TOP = "phase1-evidence-export"


def git_sha(repo):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def export(store, cfg, now=None, repo=None):
    now = now or time.time()
    repo = repo or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    cov, cons, _ = write_reports(store, cfg, now)
    with store.lock:
        sessions = store.db.execute(
            "select started, ended, mode, stopped_by from sessions"
        ).fetchall()
        audit = store.db.execute(
            "select venue, endpoint, method, url, params, body, request_at, response_at,"
            " status, duration_ms, sha256, size, headers, error, attempt, rate_limited"
            " from audit order by id"
        ).fetchall()
    cols = (
        "venue endpoint method url params body request_at response_at status "
        "duration_ms sha256 size headers error attempt rate_limited"
    ).split()
    audit_rows = [dict(zip(cols, r)) for r in audit]
    files = {}  # archive path -> bytes or source path

    def add_bytes(path, data):
        files[path] = data if isinstance(data, bytes) else data.encode("utf-8")

    for sub in ("raw", "normalized"):
        base = os.path.join(store.root, sub)
        for folder, _, names in os.walk(base):
            for n in sorted(names):
                if n.endswith(".tmp"):
                    continue
                src = os.path.join(folder, n)
                rel = os.path.relpath(src, store.root).replace(os.sep, "/")
                files[rel] = src
    errors = [r for r in audit_rows if r["error"]]
    add_bytes(
        "errors/http_errors.jsonl",
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in errors),
    )
    add_bytes(
        "audit.jsonl",
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in audit_rows),
    )
    add_bytes(
        "coverage_report.json",
        json.dumps(cov, indent=1, ensure_ascii=False, default=str),
    )
    add_bytes("consistency_report.json", json.dumps(cons, indent=1, ensure_ascii=False))
    sources = dict(
        recorder_version=VERSION,
        git_commit=git_sha(repo),
        python=sys.version.split()[0],
        platform=platform.system(),
    )
    add_bytes("source_versions.json", json.dumps(sources, indent=1))

    def digest(v):
        h = hashlib.sha256()
        if isinstance(v, bytes):
            h.update(v)
        else:
            with open(v, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        return h.hexdigest()

    hashes = {p: digest(v) for p, v in sorted(files.items())}
    warnings = []
    for v, x in cov["venues"].items():
        if x["connection"] != "BAĞLANDI":
            warnings.append("%s: %s" % (x["label"], x["connection"]))
        for asset, m in x["markets"].items():
            if m["missing_candle_hours"]:
                warnings.append(
                    "%s %s: %d eksik mum saati"
                    % (x["label"], asset, m["missing_candle_hours"])
                )
            if m["rate_unit"] != "OBSERVED":
                warnings.append(
                    "%s %s: oran birimi %s" % (x["label"], asset, m["rate_unit"])
                )
    if not cons["ok"]:
        warnings.append("Tutarlılık kontrolünde sorun var (consistency_report.json)")
    manifest = dict(
        recorder_version=VERSION,
        git_commit=sources["git_commit"],
        created_at=iso(now),
        capture_start=iso(min((s[0] for s in sessions), default=None)),
        capture_end=iso(max((s[1] or s[0] for s in sessions), default=None)),
        sessions=[
            dict(started=iso(a), ended=iso(b), mode=c, stopped_by=d)
            for a, b, c, d in sessions
        ],
        venues=cfg["venues"],
        markets={v: sorted(x["markets"]) for v, x in cov["venues"].items()},
        endpoints=sorted({r["endpoint"] for r in audit_rows}),
        urls=sorted({r["url"] for r in audit_rows}),
        fixture_count=cons["records"],
        raw_count=cons["raw_files"],
        files=hashes,
        warnings=warnings,
        note=(
            "Yalnızca public piyasa verisi. Hesap, anahtar veya kişisel veri içermez. "
            "Eksik ve doğrulanmamış alanlar null/UNKNOWN olarak bırakıldı."
        ),
    )
    add_bytes("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
    checks = "".join(
        "%s  %s\n" % (digest(files[p]), p)
        for p in sorted(files)
        if p != "checksums.sha256"
    )
    add_bytes("checksums.sha256", checks)
    name = "phase1-evidence-%s.zip" % time.strftime("%Y-%m-%d-%H%M%S", time.gmtime(now))
    path = os.path.join(store.root, "exports", name)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(files):
            v = files[p]
            if isinstance(v, bytes):
                z.writestr(TOP + "/" + p, v)
            else:
                z.write(v, TOP + "/" + p)
    os.replace(tmp, path)
    return path, manifest


def verify_export(path):
    """[problem] for a zip whose files do not match its checksums."""
    problems = []
    with zipfile.ZipFile(path) as z:
        sums = z.read(TOP + "/checksums.sha256").decode().splitlines()
        for line in sums:
            digest, rel = line.split("  ", 1)
            try:
                data = z.read(TOP + "/" + rel)
            except KeyError:
                problems.append("eksik: " + rel)
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                problems.append("hash uyuşmuyor: " + rel)
    return problems
