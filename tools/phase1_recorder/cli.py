"""Command line.

python -m tools.phase1_recorder --once                 one pass of every task
python -m tools.phase1_recorder --duration-hours 24    a 24-hour session
python -m tools.phase1_recorder --status               report from the records
python -m tools.phase1_recorder --validate-only        offline checks, no network
python -m tools.phase1_recorder --export               one zip of everything
python -m tools.phase1_recorder --stop                 ask a running session to stop
"""

import argparse
import json
import os
import sys

from . import VERSION
from .config import load


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m tools.phase1_recorder",
        description="Neutralbasis Phase 1 yerel piyasa kanıtı kaydedici (yalnız public okuma).",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once", action="store_true", help="Bağlantı kontrolü ve tek kayıt turu"
    )
    mode.add_argument("--duration-hours", type=float, help="Bu kadar saat kayıt")
    mode.add_argument("--status", action="store_true", help="Kayıtların durum raporu")
    mode.add_argument(
        "--validate-only", action="store_true", help="API'ye bağlanmadan doğrula"
    )
    mode.add_argument("--export", action="store_true", help="Tek ZIP paketi üret")
    mode.add_argument("--stop", action="store_true", help="Çalışan oturuma dur de")
    p.add_argument("--root", help="Kayıt klasörü (varsayılan phase1-evidence)")
    p.add_argument("--config", help="JSON ayar dosyası")
    p.add_argument(
        "--venues", help="Virgülle borsalar: Hyperliquid,LighterCore,Lighter,Extended"
    )
    p.add_argument("--assets", help="Virgülle varlıklar (varsayılan proje listesi)")
    p.add_argument("--all-markets", action="store_true", help="Tüm aktif piyasalar")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main(argv=None, transport=None):
    # Windows consoles may not use UTF-8: never fail on a Turkish character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    cfg = load(
        args.config,
        root=args.root,
        venues=args.venues.split(",") if args.venues else None,
        assets=args.assets.split(",") if args.assets else None,
        all_markets=True if args.all_markets else None,
    )
    from .clients import ALL_VENUES

    unknown = [v for v in cfg["venues"] if v not in ALL_VENUES]
    if unknown:
        print("Bilinmeyen borsa: " + ", ".join(unknown), file=sys.stderr)
        return 2
    if args.stop:
        os.makedirs(cfg["root"], exist_ok=True)
        open(os.path.join(cfg["root"], "STOP"), "w").close()
        print("Durdurma isteği yazıldı; oturum mevcut isteği bitirip kapanır.")
        return 0
    from .storage import Store
    from .validation import write_reports

    if args.status or args.validate_only:
        store = Store(cfg["root"])
        try:
            _, cons, text = write_reports(
                store, cfg, with_consistency=args.validate_only
            )
        finally:
            store.close()
        print(text)
        return 0 if cons is None or cons["ok"] else 1
    if args.export:
        from .export import export, verify_export

        store = Store(cfg["root"])
        try:
            path, manifest = export(store, cfg)
        finally:
            store.close()
        problems = verify_export(path)
        print("Paket: %s" % path)
        print(
            "Dosya: %d, uyarı: %d, sağlama: %s"
            % (
                len(manifest["files"]),
                len(manifest["warnings"]),
                "TAMAM" if not problems else problems,
            )
        )
        return 0 if not problems else 1
    from .recorder import Recorder

    if args.once:
        from .config import ONCE_OVERRIDES

        user = {}
        if args.config:
            with open(args.config, encoding="utf-8") as f:
                user = json.load(f)
        cfg.update({k: v for k, v in ONCE_OVERRIDES.items() if k not in user})
    rec = Recorder(
        cfg, transport=transport, console=lambda line: print(line, flush=True)
    )
    try:
        stopped = rec.run("once" if args.once else "duration", args.duration_hours)
        _, _, text = write_reports(rec.store, cfg, with_consistency=False)
    finally:
        rec.store.close()
    print(text)
    print("Oturum sonu: %s" % stopped)
    return 0
