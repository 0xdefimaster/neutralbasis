"""NeutralBasis Doctor checks (Phase 14 §3): connection, data, integrity, strategy,
system. OFFLINE by design: every check reads saved artifacts and local system state;
none opens a network connection (tools/nb_doctor.py runs them under
v2.control.netguard.offline()).

Offline, nothing about a venue can be CURRENT: a past live observation is reported as
HISTORICAL_ONLY with its time, never as HEALTHY. MOCK-only capabilities are UNVERIFIED,
documented-only ones NOT_TESTED, policy-forbidden ones NOT_SUPPORTED.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from v2 import paths, storage
from v2.control import capability, model

LEVEL_TO_STATUS = dict(LIVE_OBSERVED="HISTORICAL_ONLY", VERIFIED="HISTORICAL_ONLY",
                       MOCK_TESTED="UNVERIFIED", DOCUMENTED="NOT_TESTED",
                       UNVERIFIED="UNVERIFIED", UNSUPPORTED="NOT_SUPPORTED")
CAP_DIMENSION = dict(rest="connectivity", websocket="connectivity", orderbook="freshness",
                     public_trades="freshness", funding_current="freshness",
                     funding_history="funding_evidence", mark_index="freshness",
                     fees="execution_model", source_timestamps="integrity",
                     instrument_identity="integrity", settlement_evidence="funding_evidence",
                     execution_model="execution_model")
DISK_WARN_GB, DISK_FAIL_GB = 10.0, 3.0


def control_dir():
    d = paths.bench("v2-paper-data", "control")
    return d


def connection_and_data(now, matrix):
    out = []
    for v, m in matrix.items():
        for cname, c in m["capabilities"].items():
            st = LEVEL_TO_STATUS[c["level"]]
            age = None if c.get("as_of") is None else now - c["as_of"]
            reason = "%s: %s" % (c["level"], c.get("value"))
            if c["level"] == "LIVE_OBSERVED" and age is not None:
                reason += " (last seen %.1f h ago; offline doctor cannot prove it is " \
                          "current)" % (age / 3600.0)
            elif c["level"] == "LIVE_OBSERVED":
                reason += " (observed per %s; time not recorded; historical)" % c.get("source")
            out.append(model.record(
                v, CAP_DIMENSION[cname], st, reason, channel=cname,
                evidence_level=c["level"], last_message_at=c.get("as_of"),
                data_source="HISTORICAL" if c["level"] in ("LIVE_OBSERVED", "VERIFIED")
                else "MOCK" if c["level"] == "MOCK_TESTED" else "HISTORICAL",
                next_action=None if st == "HISTORICAL_ONLY" and not c.get("current")
                else ("run an approved live check" if st in ("UNVERIFIED", "NOT_TESTED")
                      else None)))
    return out


def integrity(now, bench=None):
    """From saved WS probe reports: resyncs, stale, duplicates, REST/WS agreement."""
    bench = bench or paths.bench()
    out = []
    reps = sorted(bench.glob("v2-paper-data/ws-probe-*/ws_probe_report.json"))
    if not reps:
        return [model.record("all", "integrity", "NOT_TESTED", "no live probe report found")]
    r = json.loads(reps[-1].read_text(encoding="utf-8"))
    for v, x in (r.get("results") or {}).items():
        if "error" in x:
            out.append(model.record(v, "integrity", "NOT_TESTED", x["error"][:120]))
            continue
        seqless = v == "hyperliquid"
        st = "INTEGRITY_FAILURE" if x.get("resyncs", 0) > 5 else (
            "DEGRADED" if x.get("resyncs", 0) else "HISTORICAL_ONLY")
        ing = x.get("ingest")
        if ing is None:
            ing_note = "callback errors not recorded (report older than Phase 15 P4)"
        elif not ing.get("telemetry_complete", False):
            # Phase 15 P4: telemetry incomplete -> integrity is not counted as verified
            cb = ing.get("callback") or {}
            st = "INTEGRITY_FAILURE" if st == "INTEGRITY_FAILURE" else "UNVERIFIED"
            ing_note = "TELEMETRY INCOMPLETE: %s callback errors (last: %s)" % (
                cb.get("total"), cb.get("last_error"))
        else:
            ing_note = "0 callback errors"
        out.append(model.record(
            v, "integrity", st,
            "resyncs %s, stale %s, duplicates %s, REST/WS %s; %s; %s" % (
                x.get("resyncs"), x.get("stale"), x.get("duplicates"), x.get("rest_vs_ws"),
                "no sequence numbers: long gaps are only ESTIMATED loss" if seqless
                else "sequence continuity checked", ing_note),
            channel="ws", evidence_level="LIVE_OBSERVED", observed_at=r.get("created"),
            sample_count=sum((x.get("messages") or {}).values()),
            clock_confidence="LOW (local clock not synchronized at probe time)",
            data_source="HISTORICAL", next_action="re-run the approved WS probe"))
    return out


def strategy(bench=None):
    bench = bench or paths.bench()
    reps = sorted(bench.glob("v2-paper-data/phase1*-scan-*/opportunity_report.json"),
                  key=lambda p: p.stat().st_mtime)
    if not reps:
        return [model.record("all", "execution_model", "NOT_TESTED", "no scanner report")], None
    r = json.loads(reps[-1].read_text(encoding="utf-8"))
    out = []
    d, e, x = r["DISCOVERY"], r["ECONOMIC"], r["EXECUTION_READINESS"]
    for s in ("funding", "price_gap", "hybrid"):
        disc = d["by_strategy"].get(s, 0)
        econ = e["by_strategy"].get(s, 0)
        stress = e["stress_positive_by_strategy"].get(s, 0)
        est = x["estimated_by_strategy"].get(s, 0)
        st = "HISTORICAL_ONLY" if disc else "NOT_TESTED"
        out.append(model.record(
            "all", "execution_model", st,
            "%s: discovered %d, expected-net positive %d, stress positive %d, ESTIMATED "
            "candidates %d, STRICT eligible 0 (snapshot %s UTC)" % (
                s, disc, econ, stress, est,
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.get("at") or 0))),
            channel="strategy:" + s, evidence_level="ESTIMATED",
            observed_at=r.get("at"), data_source="ESTIMATED",
            next_action="realized funding / persistence evidence (see Funding Persistence)"
            if s != "price_gap" else "price history and a US-session snapshot"))
    return out, dict(report=str(reps[-1]), at=r.get("at"), counts=r.get("counts"),
                     strict_first=r.get("strict_first_rejection"),
                     estimated_first=r.get("estimated_first_rejection"))


def memory():
    if os.name == "nt":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPhys / 2 ** 30, m.ullAvailPhys / 2 ** 30
        return None, None
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        kb = lambda k: float(info[k].strip().split()[0])  # noqa: E731
        return kb("MemTotal") / 2 ** 20, kb("MemAvailable") / 2 ** 20
    except Exception:
        return None, None


def clock_status():
    """Local query only (no network): Windows Time sync state; the offset itself needs
    a network measurement and is therefore UNKNOWN offline."""
    if os.name != "nt":
        return model.record("system", "integrity", "NOT_TESTED", "clock check implemented "
                            "for Windows only (use timedatectl on Linux)", channel="clock")
    try:
        out = subprocess.run(["w32tm", "/query", "/status"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception as ex:
        return model.record("system", "integrity", "NOT_TESTED", "w32tm failed: %s" % ex,
                            channel="clock")
    last = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                 if l.startswith("Last Successful Sync Time")), None)
    if not last:
        return model.record("system", "integrity", "UNVERIFIED",
                            "Windows Time not synchronized or not readable", channel="clock",
                            clock_confidence="NONE")
    return model.record("system", "integrity", "UNVERIFIED",
                        "Windows Time synced at %s; offset not measurable offline (needs NTP)"
                        % last, channel="clock", clock_confidence="UNKNOWN_OFFSET",
                        evidence_level="LOCAL_QUERY")


def heartbeat_status(name, now, max_age_s=120.0):
    f = control_dir() / ("heartbeat-%s.json" % name)
    if not f.exists():
        return model.record("system", "storage_recorder", "DISCONNECTED",
                            "%s: no heartbeat file (not running)" % name, channel=name,
                            next_action="start only with the owner's approval")
    try:
        hb = json.loads(f.read_text(encoding="utf-8"))
        age = now - float(hb.get("at", 0))
    except Exception as ex:
        return model.record("system", "storage_recorder", "UNVERIFIED",
                            "%s heartbeat unreadable: %s" % (name, ex), channel=name)
    st = "STALE" if age > max_age_s else "HEALTHY"
    return model.record("system", "storage_recorder", st,
                        "%s heartbeat %.0f s old" % (name, age), channel=name,
                        last_message_at=hb.get("at"), evidence_level="LOCAL_HEARTBEAT",
                        data_source="OBSERVED")


def ingest_health(now, folder=None):
    """Phase 15 P4: from every heartbeat that carries them, callback-error and
    cardinality records. A callback error makes the telemetry INCOMPLETE: its integrity
    figures are not counted as verified."""
    folder = Path(folder) if folder else control_dir()
    out = []
    for f in sorted(folder.glob("heartbeat-*.json")):
        name = f.stem[len("heartbeat-"):]
        try:
            hb = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        ing, tel = hb.get("ingest"), hb.get("telemetry")
        if ing:
            cb = ing.get("callback") or {}
            if cb.get("in_window"):
                st, why = "DEGRADED", "%d callback errors in %.0f s (%.2f/min), last %s: %s" % (
                    cb["in_window"], cb.get("window_s") or 0, cb.get("per_minute") or 0,
                    cb.get("last_at"), cb.get("last_error"))
            elif cb.get("total"):
                st, why = "UNVERIFIED", "telemetry INCOMPLETE: %d callback errors since %s " \
                    "(none in the last %.0f s)" % (cb["total"], cb.get("first_at"),
                                                   cb.get("window_s") or 0)
            else:
                st, why = "HEALTHY", "0 callback errors"
            out.append(model.record(
                "system", "integrity", st, why, channel="telemetry:%s" % name,
                last_message_at=hb.get("at"), evidence_level="LOCAL_HEARTBEAT",
                data_source="OBSERVED", next_action=None if st == "HEALTHY" else
                "fix the telemetry hook; integrity figures of this process are unverified"))
            m = (ing.get("metrics") or {})
            ev = sum((m.get(k) or {}).get("evictions", 0) for k in ("per_market", "totals"))
            oc = sum((m.get(k) or {}).get("over_cap", 0) for k in ("per_market", "totals"))
            refused = (ing.get("state") or {}).get("refused_new", 0)
            out.append(model.record(
                "system", "storage_recorder", "DEGRADED" if oc or refused else "HEALTHY",
                "book metrics: %s; evictions %d, over cap %d; new state refused %d" % (
                    ", ".join("%s %s/%s" % (k, (m.get(k) or {}).get("keys"),
                                            (m.get(k) or {}).get("max_keys"))
                              for k in ("per_market", "totals")), ev, oc, refused),
                channel="cardinality:%s" % name, evidence_level="LOCAL_HEARTBEAT",
                data_source="OBSERVED", last_message_at=hb.get("at")))
        if tel:
            out.append(model.record(
                "system", "storage_recorder", "DEGRADED" if tel.get("over_cap") else "HEALTHY",
                "telemetry keys %s/%s, evictions %s, over cap %s" % (
                    tel.get("keys"), tel.get("max_keys"), tel.get("evictions"),
                    tel.get("over_cap")), channel="telemetry_keys:%s" % name,
                evidence_level="LOCAL_HEARTBEAT", data_source="OBSERVED",
                last_message_at=hb.get("at")))
    return out


def rate_budget(now, folder=None):
    """Phase 15 P2: the cross-process rate budget per scope (read from its state files;
    no venue call). The share of the venue quota used by OTHER clients on the same
    public IP is always UNKNOWN and said so."""
    from v2 import ratecoord, ratelimit
    folder = Path(folder) if folder else control_dir() / "ratecoord"
    on = ratecoord.enabled()
    if not folder.exists():
        return [model.record("system", "connectivity", "NOT_TESTED",
                             "shared rate budget %s; no state yet (no request made through "
                             "it). External clients on the same IP: UNKNOWN" % (
                                 "ENABLED" if on else "OFF (NB_V2_SHARED_BUDGET=0: "
                                 "per-process budgets)"), channel="rate_budget")]
    coord = ratecoord.Coordinator.__new__(ratecoord.Coordinator)
    coord.folder, coord.limits = folder, dict(ratelimit.BUDGETS)
    out = []
    for scope, x in coord.snapshot().items():
        if x.get("status") == "UNREADABLE":
            out.append(model.record("system", "connectivity", "UNVERIFIED",
                                    "%s: state unreadable" % scope, channel="rate:" + scope))
            continue
        c = x.get("counters") or {}
        st = ("RATE_LIMITED" if x.get("breaker") == "OPEN" or (x.get("pause_until") or 0) > now
              else "DEGRADED" if c.get("http_429") or c.get("lock_timeouts") or
              c.get("state_resets") else "HEALTHY")
        out.append(model.record(
            "system", "connectivity", st,
            "%s: tokens %.1f/%s per %ss, breaker %s, in flight %s, granted %s, 429 %s, "
            "failures %s, breaker refusals %s, leases reaped %s, state resets %s; %s" % (
                scope, x.get("tokens") or 0, x.get("limit"), x.get("window_s"),
                x.get("breaker"), x.get("in_flight"), c.get("granted"), c.get("http_429"),
                c.get("failures"), c.get("breaker_refusals"), c.get("leases_reaped"),
                c.get("state_resets"), x.get("external_ip_share")),
            channel="rate:" + scope, evidence_level="LOCAL_STATE", data_source="OBSERVED"))
    return out


def system(now):
    out = []
    out.append(model.record("system", "storage_recorder", "HEALTHY" if sys.version_info >= (3, 13)
                            else "DEGRADED", "Python %s on %s" % (platform.python_version(),
                                                                  platform.platform()),
                            channel="python", evidence_level="LOCAL", data_source="OBSERVED"))
    tot, avail = memory()
    out.append(model.record("system", "storage_recorder", "HEALTHY" if avail and avail > 1
                            else "DEGRADED" if avail else "NOT_TESTED",
                            "CPU %s logical; RAM %s GB total, %s GB free" % (
                                os.cpu_count(), None if tot is None else round(tot, 1),
                                None if avail is None else round(avail, 1)),
                            channel="cpu_ram", evidence_level="LOCAL", data_source="OBSERVED"))
    for label, p in (("C: / data root", paths.bench()),):
        try:
            du = shutil.disk_usage(p if Path(p).exists() else Path(p).anchor or "/")
            free = du.free / 2 ** 30
            st = "HEALTHY" if free >= DISK_WARN_GB else "DEGRADED" if free >= DISK_FAIL_GB \
                else "INTEGRITY_FAILURE"
            out.append(model.record("system", "storage_recorder", st,
                                    "%s: %.1f GB free of %.1f GB" % (label, free,
                                                                     du.total / 2 ** 30),
                                    channel="disk", evidence_level="LOCAL",
                                    data_source="OBSERVED",
                                    next_action=None if st == "HEALTHY" else
                                    "free space before any recording"))
        except Exception as ex:
            out.append(model.record("system", "storage_recorder", "NOT_TESTED", str(ex),
                                    channel="disk"))
    out.append(model.record("system", "storage_recorder", "NOT_SUPPORTED",
                            "D:/E: are a failing disk: never written (v2.storage)",
                            channel="failing_disk", evidence_level="POLICY"))
    for name in ("recorder", "paper_engine", "control_server"):
        out.append(heartbeat_status(name, now))
    out += ingest_health(now)
    out += rate_budget(now)
    out.append(clock_status())
    backup = Path(os.environ.get("NB_V2_BACKUP_DIR") or r"C:\Users\W10\nb-backup")
    files = [f for f in backup.rglob("*") if f.is_file()] if backup.exists() else []
    if files:
        newest = max(f.stat().st_mtime for f in files)
        out.append(model.record("system", "storage_recorder", "DEGRADED",
                                "code backups only (newest %.1f days old); the 29.6 GB "
                                "engine DB on E: has NO backup" % ((now - newest) / 86400),
                                channel="backup", last_message_at=newest,
                                evidence_level="LOCAL", data_source="OBSERVED"))
    else:
        out.append(model.record("system", "storage_recorder", "NOT_TESTED",
                                "backup folder not found", channel="backup"))
    packs = sorted(paths.bench().glob("neutralbasis-data-pack-*.zip"))
    out.append(model.record("system", "storage_recorder",
                            "HISTORICAL_ONLY" if packs else "NOT_TESTED",
                            "last data export %s" % (packs[-1].name if packs else "none"),
                            channel="export",
                            last_message_at=packs[-1].stat().st_mtime if packs else None))
    return out


def run(now=None, venue=None, market=None):
    now = now or time.time()
    mx = capability.matrix(now)
    recs = connection_and_data(now, mx) + integrity(now)
    strat, scan = strategy()
    recs += strat + system(now)
    if venue:
        recs = [r for r in recs if r["exchange_id"] in (venue, "system", "all")]
    if market:
        recs = [r for r in recs if r["market_id"] in (None, market)]
    sections = {}
    for r in recs:
        key = ("system" if r["exchange_id"] == "system" else
               "strategy" if (r["channel"] or "").startswith("strategy:") else
               "integrity" if r["dimension"] == "integrity" and r["channel"] in ("ws",)
               else r["exchange_id"])
        sections.setdefault(key, []).append(r)
    return dict(generated_at=now, mode="OFFLINE", data_source="HISTORICAL",
                note="offline doctor: venue states are historical; nothing proves a live "
                     "link", matrix=mx, records=recs, scanner=scan,
                sections={k: model.worst(r["health_status"] for r in v)
                          for k, v in sections.items()})


def save(result, keep=5):
    d = storage.check_dir(control_dir())
    d.mkdir(parents=True, exist_ok=True)
    f = d / time.strftime("doctor-%Y%m%dT%H%M%S.json", time.gmtime(result["generated_at"]))
    f.write_text(json.dumps(result, default=str), encoding="utf-8")
    (d / "doctor-latest.json").write_text(json.dumps(result, default=str), encoding="utf-8")
    old = sorted(d.glob("doctor-2*.json"))[:-keep]
    for o in old:                      # bounded: keep the last `keep` runs
        o.unlink()
    return f
