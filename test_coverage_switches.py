"""PAPER pilot coverage switches (owner decision 2026-11-11). Each is off by default;
with it on, more public markets are read. No gate is touched by these switches."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import bot
from test_feed_parsing import ListingFeed, RECORDED_AT

ROOT = Path(__file__).resolve().parent


def constant(module, name, env):
    """The module constant as a fresh interpreter sees it under `env`."""
    code = "import %s; print(%s.%s)" % (module, module, name)
    full = dict(os.environ, **env)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=full,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr


class ImportTimeSwitchTests(unittest.TestCase):
    def test_rfq_stream_limit_default_and_override(self):
        self.assertEqual(constant("extended_rfq", "STREAM_LIMIT", {"NB_RFQ_STREAMS": "12"})[1], "12")
        self.assertEqual(constant("extended_rfq", "STREAM_LIMIT", {"NB_RFQ_STREAMS": "40"})[1], "40")
        code, _, err = constant("extended_rfq", "STREAM_LIMIT", {"NB_RFQ_STREAMS": "500"})
        self.assertNotEqual(code, 0)
        self.assertIn("NB_RFQ_STREAMS out of range", err)

    def test_universe_floor_default_and_override(self):
        env = {k: v for k, v in os.environ.items() if k != "NB_UNIVERSE_MIN_USD"}
        r = subprocess.run([sys.executable, "-c", "import connectors; print(connectors.UNIVERSE_MIN_USD)"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "1000000.0")
        self.assertEqual(constant("connectors", "UNIVERSE_MIN_USD",
                                  {"NB_UNIVERSE_MIN_USD": "200000"})[1], "200000.0")
        code, _, err = constant("connectors", "UNIVERSE_MIN_USD", {"NB_UNIVERSE_MIN_USD": "10"})
        self.assertNotEqual(code, 0)
        self.assertIn("NB_UNIVERSE_MIN_USD out of range", err)


class LighterRhAllTests(unittest.TestCase):
    def hydrated(self, env):
        asked = []
        done = threading.Event()

        def record(feed, v, asset, start=None):
            asked.append((v, asset))
            raise ValueError("kayıtlı yanıt yok")  # no data: the market keeps no rate

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(bot, "now", lambda: RECORDED_AT), \
                mock.patch.object(bot.Feed, "funding_history", record):
            feed = ListingFeed()
            feed.markets()
            deadline = time.time() + 10
            while time.time() < deadline:
                with feed.rwa_funding_lock:
                    if not feed.rwa_funding_busy:
                        break
                time.sleep(0.02)
            done.set()
        return feed, asked

    def test_off_by_default_no_extra_requests(self):
        env = {"NB_LIGHTER_RH_ALL": "0"}
        feed, asked = self.hydrated(env)
        # The recorded listings share no RWA RFQ market, so nothing is hydrated.
        self.assertEqual(asked, [])

    def test_on_asks_every_listed_market_once_and_invents_no_rate(self):
        feed, asked = self.hydrated({"NB_LIGHTER_RH_ALL": "1"})
        listed = sorted(feed.meta["Lighter"])
        self.assertEqual(sorted(a for v, a in asked), listed)
        self.assertTrue(all(v == "Lighter" for v, a in asked))
        for a in listed:
            # A failed read leaves the rate unknown, never a default.
            self.assertIsNone(feed.meta["Lighter"][a]["rate"])


class ExtraVenueTests(unittest.TestCase):
    def test_extra_perp_dexes_off_by_default_and_checked(self):
        env = {k: v for k, v in os.environ.items() if k != "NB_EXTRA_PERP_DEXES"}
        r = subprocess.run([sys.executable, "-c", "import capabilities as c; print(','.join(c.PERP_DEXES))"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Aster", r.stdout)
        code, out, _ = constant("capabilities", "PERP_DEXES",
                                {"NB_EXTRA_PERP_DEXES": "Aster, Nado"})
        self.assertIn("'Aster'", out)
        self.assertIn("'Nado'", out)
        code, _, err = constant("capabilities", "PERP_DEXES", {"NB_EXTRA_PERP_DEXES": "GRVT"})
        self.assertNotEqual(code, 0)  # GRVT docs were not read: not allowed
        self.assertIn("not an allowed venue", err)

    def test_every_extra_venue_has_a_documented_periodic_model_and_ccxt_declaration(self):
        import capabilities as c

        for v in c.EXTRA_PERP_DEXES:
            model = c.funding_model(v)
            self.assertEqual(model["model"], c.PERIODIC, v)
            self.assertNotEqual(model["source"], c.DOCS_UNREAD, v)
            self.assertEqual(model["direction"], c.LONG_PAYS, v)
            self.assertTrue(c.DECLARED[v]["connector"].startswith("CCXT "), v)
            # Declaration only: runtime evidence still decides (nothing pre-verified).
            self.assertEqual(c.DECLARED[v]["caps"]["funding_history"][0], c.UNVERIFIED)
            self.assertEqual(c.DECLARED[v]["caps"]["order_placement"][0], c.NONE)


class ExtraVenueGuardTests(unittest.TestCase):
    def test_public_reads_allowed_and_trading_paths_refused(self):
        from pilot import network_guard as g

        ok = [
            ("fapi.asterdex.com", "GET", "/fapi/v1/premiumIndex"),
            ("api.pacifica.fi", "GET", "/api/v1/info/prices"),
            ("gateway.prod.nado.xyz", "POST", "/v1/query"),
            ("archive.prod.nado.xyz", "POST", "/v1"),
            ("data-api.hibachi.xyz", "GET", "/market/exchange-info"),
            ("omni.apex.exchange", "GET", "/api/v3/symbols"),
        ]
        for h, m, p in ok:
            self.assertIsNone(g.check_request(h, m, p), (h, m, p))
        refused = [
            ("gateway.prod.nado.xyz", "POST", "/v1/execute"),
            ("fapi.asterdex.com", "POST", "/fapi/v1/order"),
            ("fapi.asterdex.com", "GET", "/fapi/v2/balance"),
            ("fapi.asterdex.com", "GET", "/fapi/v2/positionRisk"),
            ("fapi.asterdex.com", "POST", "/fapi/v1/listenKey"),
            ("api.pacifica.fi", "POST", "/api/v1/orders/create"),
            ("omni.apex.exchange", "GET", "/api/v3/account"),
            ("api.hibachi.xyz", "GET", "/trade/account/info"),
        ]
        for h, m, p in refused:
            self.assertIsNotNone(g.check_request(h, m, p), (h, m, p))


class QuoteTests(unittest.TestCase):
    def test_nado_usdt0_quote_is_scanned_and_unknown_quotes_are_not(self):
        import wide_markets

        self.assertIn("USDT0", wide_markets.PERP_QUOTES)
        for q in ("USD1", "U", "EUR", "BTC"):
            self.assertNotIn(q, wide_markets.PERP_QUOTES)


class TradeXyzTests(unittest.TestCase):
    def test_xyz_markets_are_renamed_priced_and_others_dropped(self):
        import wide_markets as w

        markets = {
            "XYZ-NVDA/USDC:USDC": dict(base="XYZ-NVDA", info=dict(dex="xyz", growthMode="enabled")),
            "XYZ-GOLD/USDC:USDC": dict(base="XYZ-GOLD", info=dict(dex="xyz")),
            "KM-NVDA/USDH:USDH": dict(base="KM-NVDA", info=dict(dex="km")),
        }
        out = w.xyz_markets(markets)
        self.assertEqual(out["XYZ-NVDA/USDC:USDC"]["base"], "NVDA")
        self.assertEqual(out["XYZ-NVDA/USDC:USDC"]["taker"], w.XYZ_GROWTH_TAKER)
        self.assertEqual(out["XYZ-GOLD/USDC:USDC"]["taker"], w.XYZ_TAKER)
        self.assertEqual(out["XYZ-GOLD/USDC:USDC"]["neutralbasis_category"], "RWA")
        self.assertFalse(out["KM-NVDA/USDH:USDH"]["active"])  # another deployer: not xyz
        # CCXT hands back its cached markets: mapping twice changes nothing.
        again = w.xyz_markets(out)
        self.assertEqual(again["XYZ-NVDA/USDC:USDC"]["base"], "NVDA")
        self.assertIsNot(again["XYZ-NVDA/USDC:USDC"].get("active"), False)

    def test_tradexyz_model_documented(self):
        import capabilities as c

        self.assertIn("tradeXYZ", c.EXTRA_PERP_DEXES)
        m = c.funding_model("tradeXYZ")
        self.assertEqual(m["model"], c.PERIODIC)
        self.assertIn("Oracle", m["price"])


if __name__ == "__main__":
    unittest.main()
