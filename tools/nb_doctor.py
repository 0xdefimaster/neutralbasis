r"""NeutralBasis Doctor (Phase 14 Slice B). PAPER ONLY.

  python -m tools.nb_doctor --offline [--json] [--venue X] [--market M]
      every check from saved artifacts and local state, under a socket guard that
      FAILS the run if anything tries to open a network connection
  python -m tools.nb_doctor --status [--json]
      prints the last saved doctor result only (no checks, no network)
  python -m tools.nb_doctor --live
      REFUSED in Phase 14 (live venue checks need the owner's separate approval)

Symbols: [OK] HEALTHY, [~] HISTORICAL_ONLY / NOT_SUPPORTED, [?] NOT_TESTED / UNVERIFIED,
[!] DEGRADED / STALE / RATE_LIMITED, [X] DISCONNECTED / INTEGRITY_FAILURE.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYM = dict(HEALTHY="[OK]", HISTORICAL_ONLY="[~] ", NOT_SUPPORTED="[~] ", NOT_TESTED="[?] ",
           UNVERIFIED="[?] ", DEGRADED="[!] ", STALE="[!] ", RATE_LIMITED="[!] ",
           DISCONNECTED="[X] ", INTEGRITY_FAILURE="[X] ")


def ago(t, now):
    if not t:
        return "never"
    s = now - float(t)
    return "%.0f s ago" % s if s < 120 else "%.1f h ago" % (s / 3600)


def render(res):
    now = res["generated_at"]
    lines = ["NeutralBasis Doctor  (%s, %s UTC)  PAPER ONLY" % (
        res["mode"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))),
             res["note"], ""]
    for v, m in res["matrix"].items():
        recs = [r for r in res["records"] if r["exchange_id"] == v]
        worst = res["sections"].get(v, "NOT_TESTED")
        lines.append("%s %s  (%s%s; last verification %s)" % (
            SYM[worst], v, "PAPER" if m["paper_venue"] else "OBSERVE",
            "" if m["observation_venue"] or not m["paper_venue"] else ", not observed",
            ago(m["last_successful_verification"], now)))
        for r in recs:
            if r["channel"] == "ws":
                continue                      # shown once, under WS integrity
            lines.append("    %s %-20s %s" % (SYM[r["health_status"]], r["channel"],
                                              r["health_reason"]))
    for title, key in (("WS integrity (last approved probe)", "integrity"),
                       ("Strategies (last scanner report)", "strategy"),
                       ("System", "system")):
        recs = [r for r in res["records"] if (
            key == "integrity" and r["dimension"] == "integrity" and r["channel"] == "ws") or (
            key == "strategy" and (r["channel"] or "").startswith("strategy:")) or (
            key == "system" and r["exchange_id"] == "system")]
        lines.append("")
        lines.append("%s %s" % (SYM[res["sections"].get(key, "NOT_TESTED")], title))
        for r in recs:
            label = r["exchange_id"] if key == "integrity" else (r["channel"] or
                                                                 r["exchange_id"])
            lines.append("    %s %-16s %s" % (SYM[r["health_status"]], label,
                                              r["health_reason"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nb_doctor")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="default")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--venue")
    ap.add_argument("--market")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    from v2.control import doctor, netguard

    if a.live:
        print("refused: live venue checks need the owner's separate approval "
              "(Phase 14 runs offline only)", file=sys.stderr)
        return 2
    if a.status:
        f = doctor.control_dir() / "doctor-latest.json"
        if not f.exists():
            print("no saved doctor result; run --offline first", file=sys.stderr)
            return 1
        res = json.loads(f.read_text(encoding="utf-8"))
        res["mode"] = "STATUS (saved %s)" % res.get("generated_at")
    else:
        with netguard.offline() as attempts:
            res = doctor.run(venue=a.venue, market=a.market)
        res["network_attempts"] = attempts
        if attempts:
            print("FAILED: network access attempted offline: %s" % attempts, file=sys.stderr)
            return 3
        if not a.no_save:
            res["saved_to"] = str(doctor.save(res))
    print(json.dumps(res, indent=1, default=str) if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
