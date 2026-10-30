import itertools, json, unittest
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
        reg.observe_history({("TxFlow", "BTC", "perp"): dict(complete=True)}, T0)
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
            },
            T0,
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
        gaps = {("Hyperliquid", a): 3600 for a in ("BTC", "ETH", "SOL")}
        reg = Registry()
        rows = [row("Hyperliquid", a) for a in ("BTC", "ETH", "SOL")]
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        x = status(reg, "Hyperliquid", "funding_interval")
        self.assertEqual(x["status"], FULL)
        self.assertIn("3/3", x["method"])

    def test_a_mismatching_market_is_blocked_and_marks_the_venue(self):
        gaps = {("Hyperliquid", "BTC"): 3600, ("Hyperliquid", "ETH"): 28800}
        reg = Registry()
        rows = [row("Hyperliquid", "BTC"), row("Hyperliquid", "ETH")]
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        self.assertTrue(reg.market_ok("Hyperliquid", "BTC"))
        self.assertFalse(reg.market_ok("Hyperliquid", "ETH"))
        self.assertEqual(
            status(reg, "Hyperliquid", "funding_interval")["status"], UNVERIFIED
        )

    def test_too_few_payments_decide_nothing_and_never_pass(self):
        reg = Registry()
        # No record at all is not a pass.
        self.assertFalse(reg.market_ok("Hyperliquid", "BTC"))
        reg.check_market(
            "Hyperliquid", "BTC", 3600, lambda v, a, s: [dict(ts=T0)], T0, T0 - 86400
        )
        # Too few payments to measure the gap is not a pass either.
        self.assertFalse(reg.market_ok("Hyperliquid", "BTC"))
        self.assertEqual(
            status(reg, "Hyperliquid", "funding_interval")["status"], UNVERIFIED
        )

    def test_each_venue_is_bootstrapped_before_the_rotating_sample(self):
        venues = ("Hyperliquid", "LighterCore")
        gaps = {(v, "A%d" % i): 3600 for v in venues for i in range(40)}
        rows = [row(v, "A%d" % i) for v in venues for i in range(40)]
        reg = Registry()
        reg.check_intervals(rows, self.stored(gaps), T0, T0 - 7 * 86400)
        per = {}
        for k in reg.intervals:
            per[k.split("|")[0]] = per.get(k.split("|")[0], 0) + 1
        self.assertGreaterEqual(per["LighterCore"], cap.INTERVAL_BOOTSTRAP)
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
            {(venue, "A%d" % i, "perp"): dict(complete=True) for i in range(3)}, T0
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
            [
                "Arcus: Funding modeli bilinmiyor; yalnız araştırma",
                "Arcus: İşlem fiyatı geçmişi yok",
                # An unknown model has no payment gap to check.
                "Arcus: Funding aralığı doğrulanmadı",
                "Nowhere: yetenek kaydı yok",
            ],
        )

    def test_state_survives_a_restart(self):
        reg = self.verified("LighterCore")
        again = Registry(json.loads(json.dumps(reg.state())))
        # The history keeper has not re-reported yet: its kept records still count.
        again.observe_rows([row("LighterCore", "A%d" % i) for i in range(3)], T0)
        self.assertEqual(
            again.evaluate(T0 + 100)["LighterCore"]["strategies"]["funding"], []
        )


# --- market-level evidence (Phase 0.7) ----------------------------------------------
AT = T0 + 100


def settle(
    reg,
    venue,
    asset,
    published=0.0001,
    rate=0.0001,
    interval=3600,
    paid_every=None,
    payments=12,
    observed=4,
    at=T0,
):
    """A market's last `observed` published rates (each half an interval before its
    payment) and its stored settled payments, then the market check."""
    for h in range(observed, 0, -1):
        seen = at - h * interval + interval / 2
        reg.observe_prediction(
            row(venue, asset, raw_rate=published, interval=interval, received=seen),
            seen,
        )
    paid = [
        dict(ts=at - i * (paid_every or interval), rate=rate) for i in range(payments)
    ]
    return reg.check_market(
        venue, asset, interval, lambda v, a, s: paid, at, at - 7 * 86400
    )


def verify_market(reg, venue, asset, at=T0, **kw):
    """Complete, fresh evidence for one market."""
    reg.observe_history({(venue, asset, "perp"): dict(complete=True, ts=at)}, at)
    settle(reg, venue, asset, at=at, **kw)


def premium_heuristic(reg, venue, agree=True, at=T0):
    for h in range(cap.SIGN_MIN):
        reg.observe_sign(
            venue,
            row(venue, "S%d" % h, mark=100.1, raw_rate=0.0001 if agree else -0.0001),
            at + h,
        )


def pair(asset, long, short):
    return dict(asset=asset, long=long, short=short, strategy="funding")


def gate(reg, c, rows, at=AT, strategy="funding"):
    reg.observe_rows(list(rows.values()), at)
    return cap.pair_blockers(reg, reg.evaluate(at), strategy, c, rows, at)


def rows_for(*markets, at=AT, **kw):
    return {(v, a): row(v, a, received=at, **kw) for v, a in markets}


def full_registry(markets, at=T0):
    reg = Registry()
    for v in {v for v, _ in markets}:
        premium_heuristic(reg, v, at=at)
    for v, a in markets:
        verify_market(reg, v, a, at=at)
    # Venue-level interval and history need a few markets per venue.
    for v in {v for v, _ in markets}:
        for i in range(3):
            verify_market(reg, v, "V%d" % i, at=at)
    return reg


class FundingModelTests(unittest.TestCase):
    def test_every_initial_venue_declares_a_model(self):
        for v in cap.PERP_DEXES:
            self.assertIn(cap.funding_model(v)["model"], cap.MODELS)
        self.assertEqual(cap.funding_model("Binance")["model"], cap.UNKNOWN)
        for v in ("Hyperliquid", "LighterCore", "Lighter", "Extended"):
            m = cap.funding_model(v)
            self.assertEqual(m["model"], cap.PERIODIC)
            self.assertTrue(m["direction"] and m["cashflow"] and m["history"])
            # The docs could not be read here: the source says so.
            self.assertIn("okunamadı", m["source"])

    def test_continuous_funding_is_never_judged_by_payment_gaps(self):
        reg = Registry()
        # Paradex publishes rate samples, not payments: no gap rule is applied.
        self.assertIsNone(settle(reg, "Paradex", "BTC"))
        self.assertNotIn("Paradex|BTC", reg.intervals)
        self.assertNotIn("Paradex|BTC", reg.predictions)
        reg.check_intervals([row("Paradex")], lambda *a: [], T0, T0 - 86400)
        self.assertEqual(reg.intervals, {})
        x = status(reg, "Paradex", "funding_interval")
        self.assertEqual(x["status"], UNVERIFIED)
        self.assertIn("Sürekli tahakkuk", x["method"])

    def test_continuous_and_unknown_models_stay_research_only(self):
        markets = [("Hyperliquid", "BTC"), ("Paradex", "BTC"), ("RISEx", "BTC")]
        reg = full_registry(markets)
        rows = rows_for(*markets)
        for other in ("Paradex", "RISEx"):
            why = gate(reg, pair("BTC", "Hyperliquid", other), rows)
            self.assertTrue(why)
            self.assertTrue(any("Funding modeli" in w for w in why), why)
            self.assertTrue(any("funding modeli" in w for w in why), why)
        out = reg.evaluate(AT)
        self.assertIn("funding_model", out["Paradex"]["strategies"]["convergence"])
        self.assertNotIn("funding_model", out["Hyperliquid"]["strategies"]["funding"])


class PublishedSignTests(unittest.TestCase):
    def test_published_rates_that_match_payments_are_consistent(self):
        reg = Registry()
        settle(reg, "Hyperliquid", "BTC")
        st, n, agree, ratio, _ = reg.published_status("Hyperliquid", "BTC", AT)
        self.assertEqual((st, n, agree), ("consistent", 4, 4))
        self.assertAlmostEqual(ratio, 1.0)
        # Matched observations are consumed; a repeat check adds nothing twice.
        self.assertNotIn("Hyperliquid|BTC", reg.predictions)
        settle(reg, "Hyperliquid", "BTC", observed=0)
        self.assertEqual(reg.published_status("Hyperliquid", "BTC", AT)[1], 4)

    def test_a_disagreeing_sign_is_a_contradiction(self):
        reg = Registry()
        settle(reg, "Hyperliquid", "BTC", published=-0.0001)
        self.assertEqual(reg.published_status("Hyperliquid", "BTC", AT)[0], "inverted")
        reg = Registry()
        settle(reg, "Hyperliquid", "BTC", observed=3)
        settle(
            reg, "Hyperliquid", "BTC", observed=2, published=-0.0001, at=T0 + 3 * 3600
        )
        self.assertEqual(
            reg.published_status("Hyperliquid", "BTC", T0 + 3 * 3600)[0],
            "inconsistent",
        )

    def test_a_rate_in_other_units_than_its_payments_is_refused(self):
        reg = Registry()
        # An 8-hour rate published for an hourly payment.
        settle(reg, "Hyperliquid", "BTC", published=0.0008)
        st, _, _, ratio, _ = reg.published_status("Hyperliquid", "BTC", AT)
        self.assertEqual(st, "unit_mismatch")
        self.assertAlmostEqual(ratio, 8.0)

    def test_tiny_rates_and_unrecorded_payments_decide_nothing(self):
        reg = Registry()
        settle(reg, "Hyperliquid", "BTC", rate=1e-9)
        self.assertEqual(reg.published_status("Hyperliquid", "BTC", AT)[1], 0)
        reg = Registry()
        # The observation's payment is not in the record yet: it is kept for later.
        reg.observe_prediction(row("Hyperliquid", received=T0 + 10), T0 + 10)
        settle(reg, "Hyperliquid", "BTC", observed=0)
        self.assertIn("Hyperliquid|BTC", reg.predictions)
        st, n, *_ = reg.published_status("Hyperliquid", "BTC", AT)
        self.assertEqual((st, n), ("insufficient", 0))

    def test_old_comparisons_and_observations_expire(self):
        reg = Registry()
        settle(reg, "Hyperliquid", "BTC")
        later = T0 + cap.PUBLISHED_WINDOW + 1
        self.assertEqual(
            reg.published_status("Hyperliquid", "BTC", later)[0], "insufficient"
        )
        reg.observe_prediction(row("Hyperliquid", "ETH", received=T0), T0)
        reg.observe_prediction(
            row("Hyperliquid", "ETH", received=T0 + cap.PREDICTION_KEEP + 1),
            T0 + cap.PREDICTION_KEEP + 1,
        )
        self.assertEqual(len(reg.predictions["Hyperliquid|ETH"]), 1)

    def test_venue_sign_status_never_claims_payment_evidence(self):
        reg = full_registry([("Hyperliquid", "BTC")])
        x = status(reg, "Hyperliquid", "funding_sign", AT)
        self.assertEqual(x["status"], PARTIAL)
        self.assertIn("Sezgisel", x["method"])
        self.assertIn("ödeme kanıtı değil", x["method"])
        self.assertEqual(x["evidence"]["account"], cap.NO_ACCOUNT)
        self.assertIn("4/4", x["evidence"]["published"])


class MarketEvidenceTests(unittest.TestCase):
    """The Phase 0.7 acceptance cases: venue evidence never stands in for a market."""

    def test_verified_btc_does_not_authorize_unverified_eth(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        verify_market(reg, "LighterCore", "ETH")
        rows = rows_for(
            ("Hyperliquid", "BTC"),
            ("LighterCore", "BTC"),
            ("Hyperliquid", "ETH"),
            ("LighterCore", "ETH"),
        )
        # Hyperliquid ETH is only listed: its own history and payments are unknown.
        self.assertEqual(gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows), [])
        why = gate(reg, pair("ETH", "Hyperliquid", "LighterCore"), rows)
        self.assertIn("Hyperliquid: funding aralığı bu piyasada doğrulanmadı", why)
        self.assertIn("Hyperliquid: 30 günlük geçmiş kaydı yok", why)
        self.assertTrue(all(w.startswith("Hyperliquid: ") for w in why), why)
        # ...although the venue itself shows every capability as usable.
        self.assertEqual(reg.evaluate(AT)["Hyperliquid"]["strategies"]["funding"], [])

    def test_partial_venue_with_a_missing_candidate_interval(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        settle(reg, "LighterCore", "BAD", paid_every=28800)
        reg.observe_history({("LighterCore", "SOL", "perp"): dict(complete=True)}, T0)
        out = reg.evaluate(AT)
        self.assertEqual(
            out["LighterCore"]["caps"]["funding_interval"]["status"], PARTIAL
        )
        rows = rows_for(("Hyperliquid", "SOL"), ("LighterCore", "SOL"))
        verify_market(reg, "Hyperliquid", "SOL")
        why = gate(reg, pair("SOL", "Hyperliquid", "LighterCore"), rows)
        self.assertIn("LighterCore: funding aralığı bu piyasada doğrulanmadı", why)

    def test_insufficient_payment_count(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        settle(reg, "LighterCore", "BTC", payments=cap.INTERVAL_MIN_PAYMENTS - 1)
        why = gate(
            reg,
            pair("BTC", "Hyperliquid", "LighterCore"),
            rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC")),
        )
        self.assertIn("LighterCore: gerçekleşmiş ödeme yetersiz · 5/6", why)

    def test_incorrect_interval(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        # Published hourly, settled every 8 hours.
        settle(reg, "LighterCore", "BTC", paid_every=28800)
        why = gate(
            reg,
            pair("BTC", "Hyperliquid", "LighterCore"),
            rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC")),
        )
        self.assertIn("LighterCore: funding aralığı ödemelerle uyuşmuyor", why)
        # A row whose published interval changed since the check is re-checked first.
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        rows = rows_for(("Hyperliquid", "BTC"))
        rows[("LighterCore", "BTC")] = row("LighterCore", received=AT, interval=14400)
        why = gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows)
        self.assertIn(
            "LighterCore: yayımlanan funding aralığı değişti; yeniden doğrulanmalı", why
        )

    def test_expired_evidence(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        late = T0 + cap.EVIDENCE_EXPIRY + 1
        rows = rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC"), at=late)
        why = gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows, at=late)
        for v in ("Hyperliquid", "LighterCore"):
            self.assertIn("%s: funding aralığı kanıtının süresi doldu" % v, why)
            self.assertIn("%s: geçmiş kaydının süresi doldu" % v, why)
        # A fresh check renews it.
        for v in ("Hyperliquid", "LighterCore"):
            verify_market(reg, v, "BTC", at=late)
        self.assertEqual(
            gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows, at=late + 1), []
        )

    def test_funding_sign_disagreement(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        reg.published["LighterCore|BTC"] = []
        settle(reg, "LighterCore", "BTC", published=-0.0001)
        why = gate(
            reg,
            pair("BTC", "Hyperliquid", "LighterCore"),
            rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC")),
        )
        self.assertIn(
            "LighterCore: funding işareti çelişkili · yayımlanan oran 0/4 ödemede aynı yönde",
            why,
        )
        # The venue heuristic running against the premium blocks every market too.
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        reg.signs["LighterCore"] = {}
        premium_heuristic(reg, "LighterCore", agree=False)
        why = gate(
            reg,
            pair("BTC", "Hyperliquid", "LighterCore"),
            rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC")),
        )
        self.assertIn(
            "LighterCore: funding işareti prim yönünün tersine görünüyor", why
        )

    def test_unknown_fee(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        rows = rows_for(("Hyperliquid", "BTC"))
        rows[("LighterCore", "BTC")] = row("LighterCore", received=AT, fee=None)
        why = gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows)
        self.assertIn("LighterCore: ücret bilinmiyor", why)

    def test_supported_venue_with_an_unsupported_market(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        reg.observe_rows(
            list(rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC")).values()), AT
        )
        out = reg.evaluate(AT)
        self.assertEqual(out["LighterCore"]["strategies"]["convergence"], [])
        rows = rows_for(("Hyperliquid", "BTC"))
        # Listed by the venue, but without a contract size and never checked.
        rows[("LighterCore", "NEW")] = row(
            "LighterCore", "NEW", received=AT, contract_size=None
        )
        rows[("Hyperliquid", "NEW")] = row("Hyperliquid", "NEW", received=AT)
        verify_market(reg, "Hyperliquid", "NEW")
        why = gate(
            reg, pair("NEW", "Hyperliquid", "LighterCore"), rows, strategy="convergence"
        )
        self.assertIn("LighterCore: kontrat büyüklüğü bilinmiyor", why)
        self.assertIn("LighterCore: funding aralığı bu piyasada doğrulanmadı", why)
        # Not listed in this scan at all.
        why = gate(reg, pair("GONE", "Hyperliquid", "LighterCore"), rows)
        self.assertIn("LighterCore: piyasa bu taramada yok", why)

    def test_restart_keeps_market_evidence(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        reg.observe_prediction(row("LighterCore", "ETH", received=T0), T0)
        again = Registry(json.loads(json.dumps(reg.state())))
        rows = rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC"))
        c = pair("BTC", "Hyperliquid", "LighterCore")
        self.assertEqual(gate(again, c, rows), [])
        e = again.market_evidence(
            "LighterCore", "BTC", rows[("LighterCore", "BTC")], AT
        )
        self.assertEqual(e["interval"]["status"], "verified")
        self.assertEqual(e["interval"]["n"], 12)
        self.assertEqual(e["history"]["status"], "complete")
        self.assertEqual(e["sign"]["published"]["n"], 4)
        self.assertEqual(e["sign"]["account"]["status"], "unavailable")
        self.assertEqual(e["confidence"], "orta")
        self.assertIn("LighterCore|ETH", again.predictions)
        # Kept evidence still expires on its own clock after the restart.
        late = T0 + cap.EVIDENCE_EXPIRY + 1
        self.assertTrue(gate(again, c, rows_for(*[k for k in rows], at=late), at=late))


# Each way a market's own evidence can be missing, short, stale or contradictory.
def _drop_row(reg, rows):
    del rows[("LighterCore", "BTC")]


def _row(**kw):
    def f(reg, rows):
        rows[("LighterCore", "BTC")] = dict(rows[("LighterCore", "BTC")], **kw)

    return f


def _interval(**kw):
    def f(reg, rows):
        reg.intervals["LighterCore|BTC"].update(kw)

    return f


def _history(value):
    def f(reg, rows):
        if value is None:
            del reg.market_history["LighterCore|BTC"]
        else:
            reg.market_history["LighterCore|BTC"] = value

    return f


def _published(samples):
    def f(reg, rows):
        reg.published["LighterCore|BTC"] = samples

    return f


def _heuristic_inverted(reg, rows):
    reg.signs["LighterCore"] = {}
    premium_heuristic(reg, "LighterCore", agree=False)


def _model(model):
    def f(reg, rows):
        cap.FUNDING_MODELS["LighterCore"] = cap._model(model, direction=cap.LONG_PAYS)

    return f


def _no_convention(reg, rows):
    cap.FUNDING_MODELS["LighterCore"] = cap._model(cap.PERIODIC)


GAPS = dict(
    not_listed=_drop_row,
    stale_row=_row(received=AT - cap.FRESH - 1),
    no_contract_size=_row(contract_size=None),
    no_fee=_row(fee=None),
    no_interval=_row(interval=None),
    no_rate=_row(raw_rate=None),
    interval_changed=_row(interval=28800),
    no_interval_record=lambda reg, rows: reg.intervals.pop("LighterCore|BTC"),
    few_payments=_interval(gap=None, match=None, n=3),
    interval_mismatch=_interval(gap=28800, match=False),
    interval_expired=_interval(at=AT - cap.EVIDENCE_EXPIRY - 1),
    no_history=_history(None),
    history_incomplete=_history([T0, False]),
    history_expired=_history([AT - cap.HISTORY_EXPIRY - 1, True]),
    no_published=_published([]),
    published_inverted=_published([[T0 - i, False, 1.0] for i in range(4)]),
    published_mixed=_published([[T0 - i, i != 0, 1.0] for i in range(4)]),
    published_units=_published([[T0 - i, True, 8.0] for i in range(4)]),
    published_expired=_published(
        [[AT - cap.PUBLISHED_WINDOW - i - 1, True, 1.0] for i in range(4)]
    ),
    heuristic_inverted=_heuristic_inverted,
    model_continuous=_model(cap.CONTINUOUS),
    model_epoch=_model(cap.EPOCH),
    model_unknown=_model(cap.UNKNOWN),
    no_sign_convention=_no_convention,
)


class EveryGapStaysResearchTests(unittest.TestCase):
    def setUp(self):
        saved = dict(cap.FUNDING_MODELS)
        self.addCleanup(
            lambda: (cap.FUNDING_MODELS.clear(), cap.FUNDING_MODELS.update(saved))
        )

    def fresh(self):
        reg = full_registry([("Hyperliquid", "BTC"), ("LighterCore", "BTC")])
        return reg, rows_for(("Hyperliquid", "BTC"), ("LighterCore", "BTC"))

    def test_complete_evidence_passes(self):
        reg, rows = self.fresh()
        for strategy in ("funding", "convergence"):
            self.assertEqual(
                gate(
                    reg,
                    pair("BTC", "Hyperliquid", "LighterCore"),
                    rows,
                    strategy=strategy,
                ),
                [],
            )

    def test_any_single_gap_blocks_both_strategies(self):
        for name, gap in GAPS.items():
            for strategy in ("funding", "convergence"):
                with self.subTest(gap=name, strategy=strategy):
                    reg, rows = self.fresh()
                    saved = dict(cap.FUNDING_MODELS)
                    try:
                        gap(reg, rows)
                        why = gate(
                            reg,
                            pair("BTC", "Hyperliquid", "LighterCore"),
                            rows,
                            strategy=strategy,
                        )
                    finally:
                        cap.FUNDING_MODELS.clear()
                        cap.FUNDING_MODELS.update(saved)
                    self.assertTrue(why)
                    self.assertTrue(any(w.startswith("LighterCore") for w in why), why)

    def test_any_two_gaps_block(self):
        for (n1, g1), (n2, g2) in itertools.combinations(GAPS.items(), 2):
            with self.subTest(gaps=(n1, n2)):
                reg, rows = self.fresh()
                saved = dict(cap.FUNDING_MODELS)
                try:
                    for g in (g1, g2):
                        try:
                            g(reg, rows)
                        except KeyError:
                            pass  # the first gap already removed what the second edits
                    why = gate(reg, pair("BTC", "Hyperliquid", "LighterCore"), rows)
                finally:
                    cap.FUNDING_MODELS.clear()
                    cap.FUNDING_MODELS.update(saved)
                self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
