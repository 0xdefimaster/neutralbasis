"""Where ESTIMATED PAPER data may be written, and how (PAPER research only).

Owner decision 2026-11-12: C: has no room, so ESTIMATED PAPER ledgers, replay outputs
and bounded observation streams go to E:, in ONE dedicated new folder (default
E:\\nb-v2-phase8, or NB_V2_PAPER_DATA_DIR). E: has known bad blocks, so:

  * nothing is ever written under E:\\NeutralBasis (its paper.sqlite3 + WAL have no
    backup) or on D:;
  * no SQLite database is created by this module (append-only JSON lines only);
  * every line carries the sha256 of the previous line (a hash chain) and is flushed
    and fsync'ed, so a torn or corrupted line is DETECTED on read, never trusted;
  * files are only appended or created; nothing here deletes or overwrites.

The strict hybrid desk (v2.hybrid_desk) keeps its own rule (never on E:); this module
is for the separate, opt-in ESTIMATED PAPER data only.
"""
import hashlib
import json
import os
from pathlib import Path

ENV = "NB_V2_PAPER_DATA_DIR"
DEFAULT_ROOT = r"E:\nb-v2-phase8"
FORBIDDEN = (r"e:\neutralbasis",)          # unbacked engine data: never written
GENESIS = "0" * 64
TORN_MARK = "TORN_TAIL_SKIPPED"


class StorageError(RuntimeError):
    pass


def root(environ=None):
    env = os.environ if environ is None else environ
    return Path(env.get(ENV) or DEFAULT_ROOT)


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def check_dir(path, environ=None):
    """The resolved folder if ESTIMATED PAPER data may be written there, else raise.
    Allowed: inside the configured root (E:\\nb-v2-phase8 by default) or anywhere off
    D:/E: (tests use temporary folders). Never D:, never E:\\NeutralBasis, never
    elsewhere on E:."""
    p = _norm(path)
    if p[:2] == "d:":
        raise StorageError("D: is a failing disk: refused (%s)" % path)
    for bad in FORBIDDEN:
        if p == bad or p.startswith(bad + os.sep):
            raise StorageError("E:\\NeutralBasis holds unbacked engine data: refused")
    if p[:2] == "e:":
        r = _norm(root(environ))
        if not (p == r or p.startswith(r + os.sep)):
            raise StorageError("on E: only the dedicated folder %s is allowed (%s)"
                               % (r, path))
    return Path(p)


def _line_hash(prev, payload):
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


class ChainLog:
    """Append-only JSON lines with a sha256 chain: line = {"prev", "h", "rec"}."""

    def __init__(self, path, environ=None):
        self.path = Path(path)
        check_dir(self.path.parent, environ)
        if self.path.suffix.lower() in (".sqlite", ".sqlite3", ".db"):
            raise StorageError("no database files here")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows, report = read_chain(self.path)
        if report["status"] == "CORRUPT":
            raise StorageError("refusing to append to a corrupt chain: %s" % report)
        self.last = report["last_hash"]
        self.count = len(rows)
        self.report = report
        if report["status"] == "TORN_TAIL":
            # An unfinished last line (crash mid-write): keep it, isolate it with a
            # newline and a marker linked to the last good line. Nothing is deleted.
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(chr(10))
            self.append(dict(kind=TORN_MARK, bad_line=report["bad_line"]))

    def append(self, rec):
        payload = json.dumps(rec, sort_keys=True, default=str, separators=(",", ":"))
        h = _line_hash(self.last, payload)
        line = json.dumps(dict(prev=self.last, h=h, rec=json.loads(payload)),
                          sort_keys=True, separators=(",", ":"))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.last, self.count = h, self.count + 1
        return h


def read_chain(path):
    """(records, report). report.status: OK / EMPTY / TORN_TAIL (an unfinished last
    line, ignored) / CORRUPT (a hash or link mismatch: records stop before it)."""
    path = Path(path)
    if not path.exists():
        return [], dict(status="EMPTY", lines=0, last_hash=GENESIS)
    raw = path.read_bytes().decode("utf-8", errors="replace").split("\n")
    if raw and raw[-1] == "":
        raw = raw[:-1]
    out, prev, torn = [], GENESIS, []

    def parse(line, prev):
        try:
            x = json.loads(line)
            payload = json.dumps(x["rec"], sort_keys=True, default=str,
                                 separators=(",", ":"))
            return x if x["prev"] == prev and x["h"] == _line_hash(prev, payload) else None
        except Exception:
            return None

    for i, line in enumerate(raw):
        x = parse(line, prev)
        if x is None:
            if i == len(raw) - 1:
                return out, dict(status="TORN_TAIL", lines=len(out), last_hash=prev,
                                 bad_line=i, torn_lines=torn)
            nxt = parse(raw[i + 1], prev)
            if nxt is not None and nxt["rec"].get("kind") == TORN_MARK:
                torn.append(i)          # isolated by the writer after a crash
                continue
            return out, dict(status="CORRUPT", lines=len(out), last_hash=prev, bad_line=i,
                             torn_lines=torn)
        if x["rec"].get("kind") != TORN_MARK:
            out.append(x["rec"])
        prev = x["h"]
    return out, dict(status="OK" if out else "EMPTY", lines=len(out), last_hash=prev,
                     torn_lines=torn)
