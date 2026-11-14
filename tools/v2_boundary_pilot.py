r"""Single-boundary funding pilot (52 public reads). NOT RUN without the owner's approval.

  python tools/v2_boundary_pilot.py --preflight          offline checks only (no network)
  python tools/v2_boundary_pilot.py --run --owner-approved --out C:\...\pilot-<date>
        (also needs NB_V2_PILOT_APPROVED=1 and NB_OBSERVATION_V3=1; NB_V2 must be unset)
  python tools/v2_boundary_pilot.py --analyze C:\...\pilot-<date>   offline verdicts

Plan (docs/v2/54): around the next whole-hour funding boundary B,
  every 10 s from B-120 s to B+120 s (25 samples):
      TxFlow activeAssetCtx (NATGAS)            25 requests
      tradeXYZ metaAndAssetCtxs (dex xyz)       25 requests
  tradeXYZ fundingHistory NATGAS: once before the window, once at B+150 s   2 requests
  = 52 requests, hard-capped (a 53rd is refused). TxFlow's NATGAS index comes from the
  saved Phase 8 stream (no metadata request).
Limits: output folder on C: only (D:/E: refused), <= 10 MB, stop at B+180 s, stop when
C: free < 5 GB, a venue stops after 3 consecutive failures, any egress-guard refusal
aborts. Reads go through v2.venues.base.http_json (the P2 shared budget when
NB_V2_SHARED_BUDGET=1, recommended) under the observation_v3 egress guard.
What it can prove: TxFlow's published value at ONE boundary (reset / continue / jump,
on the venue's own times: blockTime, prev/nextFundingTime) and tradeXYZ convergence at
a second boundary. What it cannot: which TxFlow value is PAID; anything general (n=2);
latency (the local clock offset is UNKNOWN while Windows Time is not running).
PAPER ONLY: no order, no key, no signature, no account read.
"""
import argparse
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from v2 import paths, storage  # noqa: E402
from v2.venues import tradexyz, txflow  # noqa: E402

HOUR = 3600.0
WINDOW_S, EVERY_S = 120.0, 10.0
MAX_REQUESTS = 52
MAX_BYTES = 10 * 2 ** 20
MIN_FREE_GB = 5.0
MIN_LEAD_S, MAX_LEAD_S = 150.0, 1800.0
STOP_AFTER_S = 180.0
FAILS_TO_STOP = 3
APPROVAL_ENV = "NB_V2_PILOT_APPROVED"
LABEL = "BOUNDARY_PILOT (public reads; PAPER ONLY; nothing traded)"


class PilotRefused(RuntimeError):
    pass


def next_boundary(now):
    b = (math.floor(now / HOUR) + 1) * HOUR
    return b if b - now >= MIN_LEAD_S else b + HOUR


def schedule(boundary):
    """[(t, kind)] in time order: history before, 25 sample pairs, history after."""
    n = int(2 * WINDOW_S / EVERY_S) + 1
    s = [(boundary - WINDOW_S - 20, "xyz_history_before")]
    for i in range(n):
        s.append((boundary - WINDOW_S + i * EVERY_S, "sample"))
    s.append((boundary + WINDOW_S + 30, "xyz_history_after"))
    return s


def planned_requests(boundary):
    return sum(2 if k == "sample" else 1 for _, k in schedule(boundary))


def txflow_index(bench=None):
    """NATGAS index from the saved Phase 8 stream (no request), or None."""
    bench = bench or paths.bench()
    for f in sorted(bench.glob("v2-phase8-rescue-*/stream-*/stream.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line).get("rec") or {}
            if rec.get("txflow_natgas_index"):
                return str(rec["txflow_natgas_index"])
    return None


def preflight(out, now=None, environ=None, run=False):
    """[(check, PASS / WARN / BLOCK, detail)]; no network."""
    from v2 import marketdata, ratecoord
    from v2.control import doctor
    env = os.environ if environ is None else environ
    now = time.time() if now is None else now
    res = []

    def add(name, ok, detail, warn=False):
        res.append((name, "PASS" if ok else "WARN" if warn else "BLOCK", detail))

    b = next_boundary(now)
    lead = b - now
    add("plan", planned_requests(b) == MAX_REQUESTS,
        "%d requests around %s UTC" % (planned_requests(b),
                                       time.strftime("%H:%M:%S", time.gmtime(b))))
    add("timing", MIN_LEAD_S <= lead <= MAX_LEAD_S,
        "start %.0f s before the boundary (allowed %d-%d s)" % (lead, MIN_LEAD_S,
                                                               MAX_LEAD_S))
    try:
        o = storage.check_dir(Path(out))
        bad = str(Path(out).resolve())[:2].lower() in ("d:", "e:")
        add("output folder", not bad and not (o / "pilot.jsonl").exists(),
            "%s (C: only; never appended)" % o)
    except Exception as ex:
        add("output folder", False, str(ex)[:120])
    free = shutil.disk_usage(Path(out).anchor or "C:\\").free / 2 ** 30
    add("disk", free >= MIN_FREE_GB, "%.1f GB free (min %.0f)" % (free, MIN_FREE_GB))
    idx = txflow_index()
    add("txflow index", idx is not None, "NATGAS index %s from the saved stream" % idx)
    add("time basis", marketdata.TIME_BASIS == marketdata.ANSWER, marketdata.TIME_BASIS)
    try:
        sb = ratecoord.enabled(env)
    except ValueError as ex:
        sb = False
        add("shared budget", False, str(ex))
    else:
        add("shared budget", sb, "NB_V2_SHARED_BUDGET=%s (recommended 1)" % (
            env.get(ratecoord.ENV) or "0"), warn=True)
    clk = doctor.clock_status()
    add("clock", clk["clock_confidence"] not in ("NONE",), "%s: %s" % (
        clk["health_status"], clk["health_reason"][:120]), warn=True)
    if run:
        add("approval env", env.get(APPROVAL_ENV) == "1", "%s=%s" % (
            APPROVAL_ENV, env.get(APPROVAL_ENV) or "unset"))
        add("observation mode", env.get("NB_OBSERVATION_V3") == "1" and
            env.get("NB_V2") in (None, "", "0"),
            "NB_OBSERVATION_V3=1 and NB_V2 unset (read-only egress policy)")
    return res


class Pilot:
    def __init__(self, out, http, clock=time.time, sleep=time.sleep,
                 disk_free=lambda p: shutil.disk_usage(p).free, index=None):
        self.out, self.http, self.clock, self.sleep = Path(out), http, clock, sleep
        self.disk_free, self.index = disk_free, index
        self.requests = 0
        self.bytes = 0
        self.fails = {"txflow": 0, "tradexyz": 0}
        self.stopped = {}
        self.records = []
        self.abort = None

    def call(self, xid, method, url, body, headers=None):
        if self.requests >= MAX_REQUESTS:
            raise PilotRefused("request cap %d reached" % MAX_REQUESTS)
        self.requests += 1
        t0 = self.clock()
        try:
            data = self.http(xid, method, url, body, headers)
            ok, err = True, None
            self.fails[xid] = 0
        except Exception as ex:
            # pilot.network_guard.Refused (a PermissionError): never retried, abort all
            if isinstance(ex, PermissionError) and "network guard" in str(ex):
                self.abort = "EGRESS_GUARD: %s" % str(ex)[:200]
            data, ok, err = None, False, "%s: %s" % (type(ex).__name__, str(ex)[:200])
            self.fails[xid] += 1
            if self.fails[xid] >= FAILS_TO_STOP:
                self.stopped[xid] = "3 consecutive failures"
        rec = dict(xid=xid, body=body, t0=t0, t1=self.clock(), ok=ok, error=err, data=data,
                   evidence="OBSERVED")
        line = json.dumps(rec, default=str)
        self.bytes += len(line) + 1
        if self.bytes > MAX_BYTES:
            self.abort = "OUTPUT_CAP %d bytes" % MAX_BYTES
            return rec
        with open(self.out / "pilot.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.records.append(rec)
        return rec

    def run(self, boundary):
        storage.check_dir(self.out)
        if str(self.out.resolve())[:2].lower() in ("d:", "e:"):
            raise PilotRefused("output on D:/E: refused")
        self.out.mkdir(parents=True, exist_ok=True)
        if (self.out / "pilot.jsonl").exists():
            raise PilotRefused("pilot.jsonl exists: a pilot never appends to an old run")
        hl, ix = tradexyz.INFO_URL, self.index
        for t, kind in schedule(boundary):
            if self.abort:
                break
            now = self.clock()
            if now > boundary + STOP_AFTER_S:
                self.abort = "TIME_CAP"
                break
            if t > now:
                self.sleep(t - now)
            if self.disk_free(str(self.out)) / 2 ** 30 < MIN_FREE_GB:
                self.abort = "DISK_FLOOR"
                break
            if kind.startswith("xyz_history"):
                if "tradexyz" not in self.stopped:
                    start = int((boundary - 6 * HOUR) * 1000)
                    self.call("tradexyz", "POST", hl, tradexyz.funding_history("NATGAS", start))
                continue
            if "txflow" not in self.stopped:
                m, u, b = txflow.ctx_request(ix)
                self.call("txflow", m, u, b, txflow.HEADERS)
            if "tradexyz" not in self.stopped:
                self.call("tradexyz", "POST", hl, tradexyz.meta_and_ctxs())
        man = dict(label=LABEL, boundary=boundary, requests=self.requests,
                   bytes=self.bytes, stopped=self.stopped, abort=self.abort,
                   ok=sum(1 for r in self.records if r["ok"]),
                   failed=sum(1 for r in self.records if not r["ok"]))
        (self.out / "pilot_manifest.json").write_text(json.dumps(man, indent=1),
                                                      encoding="utf-8")
        return man


def analyze(folder):
    """Offline verdicts from a pilot folder (venue times; no local-clock claim)."""
    folder = Path(folder)
    man = json.loads((folder / "pilot_manifest.json").read_text(encoding="utf-8"))
    b = man["boundary"]
    recs = [json.loads(x) for x in (folder / "pilot.jsonl").read_text(
        encoding="utf-8").splitlines() if x]
    tx = []
    xyz = []
    settled = {}
    for r in recs:
        if not r["ok"]:
            continue
        d = r["data"]
        if r["xid"] == "txflow":
            n = ((d or {}).get("data") or {}).get("nodeCtx") or {}
            bt = n.get("blockTime") or ((d or {}).get("data") or {}).get("blockTime")
            if n.get("funding") is not None and bt:
                tx.append((bt / 1000.0, float(n["funding"]) / 100.0,
                           (n.get("prevFundingTime") or 0) / 1000.0))
        elif isinstance(r["body"], dict) and r["body"].get("type") == "fundingHistory":
            for x in d or []:
                settled[round(x["time"] / 1000.0 / HOUR) * HOUR] = float(x["fundingRate"])
        else:
            names = [m["name"] for m in d[0]["universe"]]
            if "xyz:NATGAS" in names:
                xyz.append((r["t1"], float(d[1][names.index("xyz:NATGAS")]["funding"])))
    tx.sort()
    before = [x for x in tx if x[0] < b]
    after = [x for x in tx if x[0] >= b]
    out = dict(boundary=b, txflow_samples=len(tx), xyz_samples=len(xyz))
    if before and after:
        lb, fa = before[-1][1], after[0][1]
        out["txflow"] = dict(last_before=lb, first_after=fa,
                             pattern="CONTINUES" if abs(fa - lb) <= 1e-12 else "JUMPS",
                             prev_funding_time_rolled=after[-1][2] >= b,
                             payment="UNKNOWN (no account evidence)")
    xb = [v for t, v in sorted(xyz) if t < b]
    s = settled.get(b)
    if xb and s is not None:
        rel = abs(xb[-1] - s) / max(abs(s), 1e-12)
        out["tradexyz"] = dict(last_before=xb[-1], settled=s, rel_diff=rel,
                               verdict="CONVERGES" if rel < 0.01 else "DIFFERS",
                               note="xyz times are receive times (local clock UNVERIFIED)")
    else:
        out["tradexyz"] = dict(verdict="UNKNOWN", why="no sample before B or no settlement")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--owner-approved", action="store_true")
    ap.add_argument("--analyze")
    ap.add_argument("--out", default=str(paths.bench("v2-paper-data",
                                                     "pilot-" + time.strftime("%Y%m%d"))))
    a = ap.parse_args(argv)
    if a.analyze:
        print(json.dumps(analyze(a.analyze), indent=1))
        return 0
    checks = preflight(a.out, run=a.run)
    for name, st, detail in checks:
        print("%-16s %-5s %s" % (name, st, detail))
    blocked = [c for c in checks if c[1] == "BLOCK"]
    if not a.run:
        print("preflight only: %d BLOCK, nothing was sent" % len(blocked))
        return 1 if blocked else 0
    if not a.owner_approved or blocked:
        print("REFUSED: --owner-approved and every check PASS/WARN are required")
        return 2
    from pilot import network_guard
    from v2 import observation
    from v2.venues import base
    out = storage.check_dir(Path(a.out))
    out.mkdir(parents=True, exist_ok=True)
    network_guard.install(str(out / "guard-pilot.jsonl"), v2=False, mode=observation.MODE)
    p = Pilot(out, base.http_json, index=txflow_index())
    man = p.run(next_boundary(time.time()))
    print(json.dumps(man, indent=1))
    return 0 if not man["abort"] else 3


if __name__ == "__main__":
    sys.exit(main())
