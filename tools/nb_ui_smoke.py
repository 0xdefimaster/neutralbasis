"""Control Center UI smoke test (Phase 14 Slice F/G). Local only.

Drives a headless Edge/Chrome through the DevTools protocol on loopback, with real
device emulation (a plain --window-size cannot go below ~500 px). For every view and
width it records:
  - horizontal overflow: document scrollWidth > viewport width, and the offending
    elements;
  - JavaScript exceptions, console errors and CSP violations;
  - a screenshot.

  python tools/nb_ui_smoke.py --url http://127.0.0.1:8765 --out <dir>

The server must already run (tools/nb_control.py serve). The browser profile is a
fresh temp folder with background networking, sync, extensions and component updates
disabled. Only loopback is contacted by this script.
"""
import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

WIDTHS = ((360, 780, True), (390, 844, True), (768, 1024, True), (1280, 900, False))
VIEWS = ("#/", "#/venues", "#/venue/txflow", "#/markets", "#/opps", "#/opp/op-00000",
         "#/paper", "#/doctor", "#/funding", "#/history")
BROWSERS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe")

PROBE = r"""(() => {
  const vw = window.innerWidth;
  const sw = document.documentElement.scrollWidth;
  const off = [];
  for (const e of document.querySelectorAll('body *')) {
    const r = e.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1 && getComputedStyle(e).position !== 'fixed') {
      let p = e.parentElement, clipped = false;
      while (p) { const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'hidden' || o === 'scroll') { clipped = true; break; } p = p.parentElement; }
      if (!clipped) off.push((e.tagName + '.' + (e.className || '')).slice(0, 80) + ' r=' + Math.round(r.right));
    }
  }
  return JSON.stringify({vw, sw, overflow: sw > vw + 1, offenders: off.slice(0, 8),
                         text: document.getElementById('view').innerText.length,
                         cannotLoad: document.body.innerText.includes('Cannot load')});
})()"""


def browser():
    for b in BROWSERS:
        if Path(b).exists():
            return b
    raise SystemExit("no Edge/Chrome found")


async def run(url, out, wait_s):
    import aiohttp

    prof = Path(tempfile.mkdtemp(prefix="nb-ui-prof-"))
    proc = subprocess.Popen([browser(), "--headless=new", "--disable-gpu", "--no-first-run",
                             "--disable-background-networking", "--disable-component-update",
                             "--disable-sync", "--disable-extensions", "--disable-default-apps",
                             "--hide-scrollbars", "--remote-debugging-address=127.0.0.1",
                             "--remote-debugging-port=0", "--user-data-dir=%s" % prof,
                             "about:blank"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    results = []
    try:
        port_file = prof / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        req = urllib.request.Request("http://127.0.0.1:%d/json/new?about:blank" % port,
                                     method="PUT")
        target = json.loads(urllib.request.urlopen(req, timeout=10).read())
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                n = [0]
                events = []

                async def call(method, **params):
                    n[0] += 1
                    mid = n[0]
                    await ws.send_str(json.dumps(dict(id=mid, method=method, params=params)))
                    while True:
                        msg = json.loads((await ws.receive()).data)
                        if msg.get("id") == mid:
                            if "error" in msg:
                                raise RuntimeError(msg["error"])
                            return msg.get("result")
                        events.append(msg)

                for m in ("Page.enable", "Runtime.enable", "Log.enable"):
                    await call(m)
                for w, h, mobile in WIDTHS:
                    await call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                               deviceScaleFactor=1, mobile=mobile)
                    for v in VIEWS:
                        events.clear()
                        await call("Page.navigate", url=url.rstrip("/") + "/" + v)
                        await asyncio.sleep(wait_s)
                        r = await call("Runtime.evaluate", expression=PROBE, returnByValue=True)
                        probe = json.loads(r["result"]["value"])
                        errs = [e for e in events if e.get("method") in (
                            "Runtime.exceptionThrown",) or (
                            e.get("method") == "Log.entryAdded" and
                            e["params"]["entry"].get("level") == "error") or (
                            e.get("method") == "Runtime.consoleAPICalled" and
                            e["params"].get("type") == "error")]
                        shot = await call("Page.captureScreenshot", format="png")
                        name = "%s-%d.png" % (v.strip("#/").replace("/", "_") or "home", w)
                        (out / name).write_bytes(base64.b64decode(shot["data"]))
                        results.append(dict(view=v, width=w, screenshot=name, errors=[
                            json.dumps(e["params"])[:300] for e in errs], **probe))
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--out", required=True)
    ap.add_argument("--wait", type=float, default=1.5)
    a = ap.parse_args(argv)
    if not a.url.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
        raise SystemExit("loopback URLs only")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(run(a.url, out, a.wait))
    (out / "ui_smoke.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    bad = [r for r in res if r["overflow"] or r["errors"] or r["cannotLoad"] or r["text"] < 20]
    for r in res:
        print("%-16s %5d  overflow=%-5s errors=%d text=%d %s" % (
            r["view"], r["width"], r["overflow"], len(r["errors"]), r["text"],
            "; ".join(r["offenders"][:3])))
    print("%d checks, %d problems" % (len(res), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
