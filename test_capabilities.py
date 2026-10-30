import unittest
import capabilities as cap
from capabilities import Registry, FULL, PARTIAL, NONE, UNVERIFIED

T0 = 2_000_000_000


def row(venue, asset="BTC", **kw):
    r = dict(
        venue=venue,
        asset=asset,
        kind="perp",
        mark=100.0,
        index=100.0,
        raw_rate=0.0001,
        interval=3600,
        fee=0.0004,
        fee_source="public",
        contract_size=1.0,
        oi=1e6,
        volume=1e7,
        received=T0,
    )
    r.update(kw)
    return r


def status(reg, venue, capability, at=T0):
    return reg.evaluate(at)[venue]["caps"][capability]


class DeclarationTests(unittest.TestCase):
    def test_every_initial_venue_declares_every_capability(self):
        reg = Registry().evaluate(T0)
        for v in cap.PERP_DEXES:
            self.assertEqual(set(reg[v]["caps"]), set(cap.CAPABILITIES))
            for x in reg[v]["caps"].values():
                self.assertIn(x["status"], cap.STATUSES)

    def test_nothing_is_eligible_without_evidence(self):
        reg = Registry().evaluate(T0)
        for v in cap.PERP_DEXES:
            self.assertTrue(reg[v]["strategies"]["funding"], v)
            self.assertTrue(reg[v]["strategies"]["convergence"], v)
        self.assertEqual(reg["Hyperliquid"]["caps"]["order_placement"]["status"], NONE)

    def test_known_limits_are_declared(self):
        reg = Registry().evaluate(T0)
        self.assertEqual(reg["Arcus"]["caps"]["last_price"]["status"], NONE)
        self.assertEqual(reg["TxFlow"]["caps"]["funding_history"]["status"], NONE)
        self.assertTrue(
            all(x["status"] == NONE for x in reg["Variational"]["caps"].values())
        )
        # Lighter RH and Lighter Core are separate venues with separate connectors.
        self.assertNotEqual(
            reg["Lighter"]["connector"], reg["LighterCore"]["connector"]
        )

    def test_evidence_never_upgrades_what_the_connector_lacks(self):
        reg = Registry()
        reg.observe_history({("TxFlow", "BTC", "perp"): dict(complete=True)})
        reg.observe_rows([row("Arcus")], T0)
        self.assertEqual(status(reg, "TxFlow", "funding_history")["status"], NONE)
        self.assertEqual(status(reg, "Arcus", "last_price")["status"], NONE)


class RowEvidenceTests(unittest.TestCase):
    def test_fresh_rows_verify_discovery_metadata_and_fees(self):
        reg = Registry()
        reg.observe_rows(
            [row("Hyperliquid"), row("Hyperliquid", "ETH", contract_size=None)], T0
        )
        caps = reg.evaluate(T0)["Hyperliquid"]["caps"]
        self.assertEqual(caps["market_discovery"]["status"], FULL)
        self.assertEqual(caps["market_discovery"]["verified_at"], T0)
        self.assertEqual(caps["contract_metadata"]["status"], PARTIAL)
        self.assertIn("1/2", caps["contract_metadata"]["limitation"])
        self.assertEqual(caps["fees"]["status"], PARTIAL)
        self.assertIn("public tarife", caps["fees"]["method"])

    def test_native_discovery_is_partial_and_stale_rows_prove_nothing(self):
        reg = Registry()
        reg.observe_rows([row("Lighter"), row("Paradex", received=T0 - 3600)], T0)
        self.assertEqual(status(reg, "Lighter", "market_discovery")["status"], PARTIAL)
        self.assertEqual(
            status(reg, "Paradex", "market_discovery")["status"], UNVERIFIED
        )

    def test_unknown_fee_is_not_supported(self):
        reg = Registry()
        reg.observe_rows([row("Hyperliquid", fee=None, fee_source=None)], T0)
        self.assertEqual(status(reg, "Hyperliquid", "fees")["status"], NONE)

    def test_a_scanned_venue_without_a_declaration_gets_generic_entries(self):
        reg = Registry()
        reg.observe_rows([row("Binance")], T0)
        out = reg.evaluate(T0)
        self.assertFalse(out["Binance"]["declared"])
        self.assertFalse(out["Binance"]["perp_dex"])
        self.assertEqual(out["Binance"]["caps"]["market_discovery"]["status"], FULL)


class BookEvidenceTests(unittest.TestCase):
    def test_book_reads_set_and_expire_the_status(self):
        reg = Registry()
        reg.observe_book("Paradex", True, T0)
        self.assertEqual(status(reg, "Paradex", "order_book")["status"], PARTIAL)
        later = T0 + cap.BOOK_FRESH + 1
        self.assertEqual(
            status(reg, "Paradex", "order_book", later)["status"], UNVERIFIED
        )
        reg.observe_book("Paradex", False, T0 + 10, ValueError("Defter eski"))
        x = status(reg, "Paradex", "order_book", T0 + 20)
        self.assertEqual(x["status"], PARTIAL)  # the last good read is still fresh
        reg = Registry()
        reg.observe_book("Paradex", False, T0, ValueError("Defter eski"))
        x = status(reg, "Paradex", "order_book")
        self.assertEqual(x["status"], UNVERIFIED)
        self.assertIn("Defter eski", x["method"])


class HistoryEvidenceTests(unittest.TestCase):
    def test_complete_share_decides(self):
        reg = Registry()
        reg.observe_history(
            {
                ("Hyperliquid", "BTC", "perp"): dict(complete=True),
                ("Hyperliquid", "ETH", "perp"): dict(complete=False),
                ("Paradex", "BTC", "perp"): dict(complete=True),
                ("RISEx", "BTC", "perp"): dict(complete=False),
            }
        )
        self.assertEqual(
            status(reg, "Hyperliquid", "funding_history")["status"], PARTIAL
        )
        self.assertEqual(status(reg, "Paradex", "funding_history")["status"], FULL)
        self.assertEqual(status(reg, "RISEx", "funding_history")["status"], UNVERIFIED)


class IntervalTests(unittest.TestCase):
    def stored(self, gaps):
        def f(v, a, start):
            g = gaps[(v, a)]
            return [dict(ts=T0 - i * g) for i in range(12)]

        return f

    def test_settled_gaps_verify_the_published_interval(self):
        gaps = {("Paradex", a): 3600 for a in ("BTC", "ETH", "SOL")}
        reg = Registry()
        rows = [row("Paradex", a) for a in ("BTC", "ETH", "SOL")]
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        x = status(reg, "Paradex", "funding_interval")
        self.assertEqual(x["status"], FULL)
        self.assertIn("3/3", x["method"])

    def test_a_mismatching_market_is_blocked_and_marks_the_venue(self):
        gaps = {("Paradex", "BTC"): 3600, ("Paradex", "ETH"): 28800}
        reg = Registry()
        rows = [row("Paradex", "BTC"), row("Paradex", "ETH")]
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        self.assertTrue(reg.market_ok("Paradex", "BTC"))
        self.assertFalse(reg.market_ok("Paradex", "ETH"))
        self.assertEqual(
            status(reg, "Paradex", "funding_interval")["status"], UNVERIFIED
        )

    def test_too_few_payments_decide_nothing(self):
        reg = Registry()
        reg.check_market(
            "Paradex", "BTC", 3600, lambda v, a, s: [dict(ts=T0)], T0, T0 - 86400
        )
        self.assertTrue(reg.market_ok("Paradex", "BTC"))
        self.assertEqual(
            status(reg, "Paradex", "funding_interval")["status"], UNVERIFIED
        )

    def test_each_venue_is_bootstrapped_before_the_rotating_sample(self):
        gaps = {(v, "A%d" % i): 3600 for v in ("X", "Y") for i in range(40)}
        rows = [row(v, "A%d" % i) for v in ("X", "Y") for i in range(40)]
        reg = Registry()
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        per = {}
        for k in reg.intervals:
            per[k.split("|")[0]] = per.get(k.split("|")[0], 0) + 1
        self.assertGreaterEqual(per["Y"], cap.INTERVAL_BOOTSTRAP)
        self.assertLessEqual(
            len(reg.intervals), 2 * cap.INTERVAL_BOOTSTRAP + cap.INTERVAL_SAMPLE
        )


class SignTests(unittest.TestCase):
    def feed(self, reg, venue, n, agree=True, premium=0.001):
        for h in range(n):
            rate = 0.0001 if agree else -0.0001
            reg.observe_rows(
                [row(venue, "A%d" % h, mark=100 * (1 + premium), raw_rate=rate)],
                T0 + h,
            )

    def test_rates_that_follow_the_premium_verify_the_sign(self):
        reg = Registry()
        self.feed(reg, "Hyperliquid", cap.SIGN_MIN)
        x = status(reg, "Hyperliquid", "funding_sign", T0 + 100)
        self.assertEqual(x["status"], PARTIAL)
        self.assertIn("10/10", x["method"])
        self.assertIn("doğrulanmadı", x["limitation"])

    def test_inverted_signs_are_not_supported(self):
        reg = Registry()
        self.feed(reg, "Paradex", cap.SIGN_MIN, agree=False)
        self.assertEqual(
            status(reg, "Paradex", "funding_sign", T0 + 100)["status"], NONE
        )

    def test_small_premiums_and_few_observations_decide_nothing(self):
        reg = Registry()
        self.feed(reg, "Paradex", cap.SIGN_MIN, premium=0.0001)
        self.assertNotIn("Paradex", reg.signs)
        self.feed(reg, "Paradex", cap.SIGN_MIN - 1)
        self.assertEqual(
            status(reg, "Paradex", "funding_sign", T0 + 100)["status"], UNVERIFIED
        )

    def test_one_observation_per_market_and_hour_and_old_ones_expire(self):
        reg = Registry()
        for _ in range(3):
            reg.observe_rows([row("Paradex", mark=100.1)], T0)
        self.assertEqual(len(reg.signs["Paradex"]), 1)
        later = T0 + cap.SIGN_WINDOW + 1
        reg.observe_rows([row("Paradex", "ETH", mark=100.1, received=later)], later)
        self.assertEqual(list(reg.signs["Paradex"]), ["ETH|%d" % (later // 3600)])


class EligibilityTests(unittest.TestCase):
    def verified(self, venue):
        reg = Registry()
        rows = [row(venue, "A%d" % i) for i in range(3)]
        reg.observe_rows(rows, T0)
        reg.observe_history(
            {(venue, "A%d" % i, "perp"): dict(complete=True) for i in range(3)}
        )
        reg.check_intervals(
            rows,
            lambda v, a, s: [dict(ts=T0 - i * 3600) for i in range(12)],
            T0,
            T0 - 7 * 86400,
        )
        for h in range(cap.SIGN_MIN):
            reg.observe_sign(venue, row(venue, "S%d" % h, mark=100.1), T0 + h)
        return reg

    def test_full_evidence_makes_a_venue_eligible(self):
        out = self.verified("Hyperliquid").evaluate(T0 + 100)
        self.assertEqual(
            out["Hyperliquid"]["strategies"], dict(funding=[], convergence=[])
        )
        self.assertEqual(cap.blockers(out, "funding", ("Hyperliquid",)), [])

    def test_blockers_name_the_missing_capabilities(self):
        reg = self.verified("Arcus")
        out = reg.evaluate(T0 + 100)
        self.assertEqual(
            cap.blockers(out, "convergence", ("Arcus", "Nowhere")),
            ["Arcus: İşlem fiyatı geçmişi yok", "Nowhere: yetenek kaydı yok"],
        )

    def test_state_survives_a_restart(self):
        reg = self.verified("Paradex")
        again = Registry(reg.state())
        again.observe_rows([row("Paradex", "A%d" % i) for i in range(3)], T0)
        again.observe_history(
            {("Paradex", "A%d" % i, "perp"): dict(complete=True) for i in range(3)}
        )
        self.assertEqual(
            again.evaluate(T0 + 100)["Paradex"]["strategies"]["funding"], []
        )


if __name__ == "__main__":
    unittest.main()
