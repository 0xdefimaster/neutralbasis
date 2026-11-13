"""NeutralBasis Control Center CLI (Phase 14). PAPER ONLY.

  python tools/nb_control.py build            build the read model (offline, guarded)
  python tools/nb_control.py serve            serve it on http://127.0.0.1:8765 (GET-only)
  python tools/nb_control.py serve --build    build first, then serve
  python tools/nb_control.py serve --rebuild-every 300
                                              rebuild offline every N s (>= 60) while
                                              serving; still no venue calls

Loopback only. There is no flag for 0.0.0.0 or a public address. Remote access later:
`tailscale serve` to this loopback port (never Funnel); see docs/v2/42.
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from v2.control import netguard, readmodel  # noqa: E402


def build():
    t = time.time()
    with netguard.offline() as attempts:
        m = readmodel.build()
    m["network_attempts"] = len(attempts)
    print("read model built in %.2f s: %s (data mode %s, %d network attempts)" % (
        time.time() - t, readmodel.readmodel_dir(), m["data_mode"], len(attempts)))
    for k, v in m["files"].items():
        print("  %-24s %8.1f KB" % (k, v / 1024.0))
    return 3 if attempts else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1",
                   help="loopback only: 127.0.0.1, ::1 or localhost")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--build", action="store_true")
    s.add_argument("--rebuild-every", type=float, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        return build()
    from v2.control.server import ControlServer, is_loopback
    if not is_loopback(a.host):
        print("refused: loopback only (got %s)" % a.host)
        return 2
    if a.build or not (readmodel.readmodel_dir() / "manifest.json").exists():
        if build():
            return 3
    srv = ControlServer(a.host, a.port)
    if a.rebuild_every:
        every = max(60.0, a.rebuild_every)

        def loop():
            while True:
                time.sleep(every)
                try:
                    build()
                except Exception as ex:  # keep serving the last good read model
                    print("rebuild failed: %s" % str(ex)[:200])
        threading.Thread(target=loop, daemon=True).start()
    print("NeutralBasis Control Center (PAPER, GET-only): http://%s:%d/" % (
        "[%s]" % a.host if ":" in a.host else a.host, srv.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
