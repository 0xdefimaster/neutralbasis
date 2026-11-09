"""Neden paper işlem açılmıyor? Çalışan motorun /api/state çıktısını okur ve girişi
engelleyen nedenleri özetler. Motoru değiştirmez, yalnızca okur.

Kullanım:
  python teshis.py                                  # yerel motor (127.0.0.1:8765)
  python teshis.py https://<adres> <panel-şifresi>  # Railway
"""

import base64, collections, json, sys, time, urllib.request

PERIODIC = ("Hyperliquid", "LighterCore", "Lighter", "Extended")


def fetch(base, password):
    req = urllib.request.Request(base.rstrip("/") + "/api/state")
    if password:
        token = base64.b64encode(("paper:" + password).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    # Tur sürerken motor yalnızca kısa bir özet döner; tam durum için yeniden denenir.
    for _ in range(20):
        with urllib.request.urlopen(req, timeout=30) as r:
            state = json.loads(r.read().decode("utf8"))
        if not state.get("busy") or state.get("wide_paper"):
            return state
        time.sleep(3)
    return state


def ago(ts):
    if not ts:
        return "hiç"
    s = time.time() - ts
    return "%.0f sn önce" % s if s < 120 else "%.0f dk önce" % (s / 60)


def top(counter, n=8, indent="    "):
    for reason, k in counter.most_common(n):
        print("%s%4d  %s" % (indent, k, reason))


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    password = sys.argv[2] if len(sys.argv) > 2 else None
    s = fetch(base, password)
    w = s.get("wide_paper") or {}
    cfg = w.get("config") or {}

    print("== Motor ==")
    print("  Otomatik giriş (running):", s.get("running"))
    print("  wide_enabled:", cfg.get("wide_enabled"),
          "· funding:", cfg.get("wide_funding_enabled"),
          "· yakınsama:", cfg.get("wide_conv_enabled"))
    print("  Son tur:", ago(w.get("last_cycle")), "· tur süresi:", w.get("cycle_seconds"))
    if w.get("error"):
        print("  HATA:", w["error"])
    print("  Açık pozisyon:", len(w.get("positions") or []),
          "· kapanmış:", len(w.get("closed") or []))
    ws = w.get("workspace") or {}
    if ws:
        print("  Çalışma alanı borsaları:", ", ".join(ws.get("venues") or []) or "-",
              "· kapalı:", ", ".join(ws.get("disabled") or []) or "-")

    f = w.get("funnel") or {}
    print("\n== Huni ==")
    print("  Perp piyasa:", f.get("markets"), "· kullanılabilir:", f.get("usable"),
          "· çift:", f.get("pairs"))
    for reason, k in (f.get("excluded") or {}).items():
        print("    dışarıda %4d  %s" % (k, reason))
    for key, x in (f.get("strategies") or {}).items():
        print("  [%s] aday %s · pozitif %s · araştırmada (kapı) %s · derin kontrol %s · bekleyen %s"
              % (key, x.get("candidates"), x.get("positive"), x.get("research"),
                 x.get("checked"), x.get("waiting")))
        print("    son tur:", x.get("round"))

    # Kapı (capability gate) nedenleri: ilk 40 adayda, bacak bazında.
    gate = collections.Counter()
    gated = clear = 0
    for c in (w.get("candidates") or []) + (w.get("conv_candidates") or []):
        if c.get("score", 0) <= 0:
            continue
        if c.get("research"):
            gated += 1
            for r in c["research"]:
                gate[r.split(" · ")[0]] += 1
        else:
            clear += 1
    print("\n== Yetenek kapısı (pozitif adaylar) ==")
    print("  Kapıdan geçen:", clear, "· kapıda kalan:", gated)
    top(gate, 15)

    rv = w.get("review") or {}
    print("\n== Derin kontrol ==")
    print("  tur %s · incelenen %s · uygun %s · atlanan %s · açılan %s"
          % (rv.get("rounds"), rv.get("examined"), rv.get("eligible"),
             rv.get("skipped"), rv.get("opened")))
    print("  Red nedenleri (toplam):")
    top(collections.Counter(rv.get("reasons") or {}), 12)

    print("\n== Funding modeli dönemsel borsalar (yalnız bunlar işlem açabilir) ==")
    caps = w.get("capabilities") or {}
    for v in PERIODIC:
        x = caps.get(v)
        if not x:
            print("  %-12s kayıt yok (taranmıyor)" % v)
            continue
        b = x.get("blocked") or {}
        print("  %-12s funding: %s" % (v, ", ".join(b.get("funding") or []) or "OK"))
        print("  %-12s yakınsama: %s" % ("", ", ".join(b.get("convergence") or []) or "OK"))

    h = s.get("history_status") or {}
    print("\n== Geçmiş kaydedici ==")
    print("  öncelikli %s (eksik %s) · piyasa %s · tam %s · hatalı %s"
          % (h.get("priority"), h.get("priority_missing"), h.get("markets"),
             h.get("complete"), h.get("failed")))


if __name__ == "__main__":
    main()
