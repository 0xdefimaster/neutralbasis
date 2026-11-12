"""Replay of the V2 recorder: integrity first, nothing accepted silently.

verify(root) checks every manifest entry against its file (present, sha256, gzip
readable, every line JSON, line count) and finds sealed files the manifest does not
list, open (.part) segments, segments flagged "recovered" after a crash, and sequence
gaps in recorded order-book deltas (per venue and market). iterate(root) yields the
records in order and raises ReplayError on the first problem unless strict=False, in
which case the problem is still reported through `issues`.
"""
import gzip
import hashlib
import json
from pathlib import Path


class ReplayError(RuntimeError):
    pass


def manifest(root):
    p = Path(root) / "manifest.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def check_segment(root, entry):
    f = Path(root) / entry["file"]
    if not f.exists():
        return "missing", None
    data = f.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        return "corrupt", "sha256 mismatch"
    try:
        lines = gzip.decompress(data).decode("utf-8").splitlines()
        recs = [json.loads(x) for x in lines if x.strip()]
    except Exception as ex:
        return "corrupt", "%s: %s" % (type(ex).__name__, str(ex)[:100])
    if entry.get("lines") is not None and len(recs) != entry["lines"]:
        return "corrupt", "line count %d != %d" % (len(recs), entry["lines"])
    return "ok", recs


def seq_gaps(records):
    """[(venue/exchange, market, prev seq, seq)] where an l2 delta does not follow."""
    last, gaps = {}, []
    for r in records:
        if r.get("kind") not in ("l2_snapshot", "l2_delta"):
            continue
        k = (r.get("venue") or r.get("exchange_id"), r.get("market_id"))
        seq, prev = r.get("seq"), r.get("prev_seq")
        if r["kind"] == "l2_delta" and k in last and prev != last[k]:
            gaps.append(k + (last[k], seq))
        if seq is not None:
            last[k] = seq
    return gaps


def verify(root):
    root = Path(root)
    out = dict(ok=True, segments=0, records=0, missing=[], corrupt=[], recovered=[],
               orphans=[], open_segments=[], seq_gaps=[])
    listed = set()
    records = []
    for e in manifest(root):
        listed.add(e["file"].replace("\\", "/"))
        out["segments"] += 1
        status, recs = check_segment(root, e)
        if status == "missing":
            out["missing"].append(e["file"])
        elif status == "corrupt":
            out["corrupt"].append(dict(file=e["file"], why=recs))
        else:
            records.extend(recs)
        if e.get("recovered"):
            out["recovered"].append(e["file"])
    for f in sorted((root / "segments").glob("*/*.jsonl.gz")):
        rel = str(f.relative_to(root)).replace("\\", "/")
        if ".part." in f.name:
            out["open_segments"].append(rel)
        elif rel not in listed:
            out["orphans"].append(rel)
    out["records"] = len(records)
    out["seq_gaps"] = seq_gaps(records)
    out["ok"] = not (out["missing"] or out["corrupt"] or out["orphans"])
    return out


def iterate(root, strict=True, issues=None):
    """Records in recorded order. strict: raise on any missing / corrupt / orphan."""
    report = verify(root)
    if issues is not None:
        issues.update(report)
    if strict and not report["ok"]:
        raise ReplayError("recorder data not intact: missing %s, corrupt %s, orphans %s"
                          % (report["missing"], report["corrupt"], report["orphans"]))
    for e in manifest(root):
        status, recs = check_segment(root, e)
        if status == "ok":
            for r in recs:
                yield r
