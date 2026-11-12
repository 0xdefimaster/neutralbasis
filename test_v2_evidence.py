"""V2 evidence tools: dataset freezing, entry-text parsing, funding forecast replay.

All series, trades and rows here are MOCK fixtures, not market evidence."""
import json
import os
import sqlite3
import tempfile
import unittest

from tools import v2_dataset, v2_evidence as ev

HOUR, DAY = 3600, 86400


def hourly(rate, start, end):
    return [(t, rate) for t in range(start + HOUR, end + 1, HOUR)]


class ParseTests(unittest.TestCase):
    def test_entry_reason_numbers(self):
        # MOCK text in the pilot's format.
        x = ev.parse_entry(
            "Funding: short Lighter, long RISEx; beklenen 168 saatlik funding +$3.95 "
            "(1g +$4.10, 3g ort. +$3.95), ... büyüklük $970, net +$1.40")
        self.assertEqual((x["horizon_hours"], x["expected_funding"], x["net"]),
                         (168, 3.95, 1.40))
        y = ev.parse_entry("Yakınsama: ...; sapma 28.1 bp (normal -14.8), ortalama "
                           "dönüş 5 saat, net -$0.51")
        self.assertEqual((y["entry_gap_bp"], y["expected_hours"], y["net"]),
                         (28.1, 5, -0.51))
        self.assertEqual(ev.parse_entry(None), {})


class ReplayTests(unittest.TestCase):
    def test_forecast_is_min_of_day1_and_day3_and_realized_is_what_was_paid(self):
        t = 20 * DAY
        # MOCK: short leg earns 2 bp/h for the last 10 days, long leg pays nothing.
        S = ev.Series(hourly(0.0002, t - 10 * DAY, t + 2 * DAY))
        L = ev.Series(hourly(0.0, t - 10 * DAY, t + 2 * DAY))
        f = ev.forecast(L, S, t, 24)
        self.assertTrue(f["eligible"])
        self.assertAlmostEqual(f["forecast"], 0.0002 * 24)
        self.assertAlmostEqual(S.sum(t, t + DAY) - L.sum(t, t + DAY), 0.0002 * 24)

    def test_a_leg_with_missing_payments_is_not_forecast(self):
        t = 20 * DAY
        S = ev.Series(hourly(0.0002, t - 2 * DAY, t))  # only 2 of 7 days
        L = ev.Series(hourly(0.0, t - 10 * DAY, t))
        self.assertIsNone(ev.forecast(L, S, t, 24))

    def test_advantage_on_a_single_day_is_not_eligible(self):
        t = 20 * DAY
        rows = hourly(0.0, t - 10 * DAY, t - DAY) + hourly(0.001, t - DAY, t)
        S, L = ev.Series(rows), ev.Series(hourly(0.0, t - 10 * DAY, t))
        self.assertFalse(ev.forecast(L, S, t, 24)["eligible"])


class DatasetTests(unittest.TestCase):
    def make_db(self, path, observer=False):
        db = sqlite3.connect(path)
        if observer:
            db.executescript(
                "create table observations(venue,asset,observed,source_ts,rate_raw,"
                "rate_unit,interval_s,next_ts,mark,index_px,bid_1k,ask_1k,label);"
                "create table rounds(started,venue,markets,errors,seconds,note);")
            db.execute("insert into observations values('TxFlow','BTC',100,100,0.001,"
                       "'percent per payment',3600,200,1.0,1.0,null,null,'OBSERVED_PUBLISHED')")
        else:
            db.executescript(
                "create table paper_trades(id,period_id,strategy,asset,long,short,opened,"
                "closed_at,size,price_pnl,funding,fees,net,reason,pending,body);"
                "create table events(id,ts,kind,body);"
                "create table ledger_events(id,event_key,ts,kind,position,venue,amount,body);"
                "create table payments(key,ts,body);"
                "create table paper_periods(id,started,ended,capital,venues,workspace,"
                "config,trades,net,source);"
                "create table hist_funding(venue,asset,ts,rate,value);"
                "create table marks(venue,asset,ts,price);"
                "create table kv(k,v);")
            db.execute("insert into paper_trades values('w1',1,'convergence','UNI',"
                       "'Extended',null,0,16,1000,1.4,0,0.5,0.9,"
                       "'Bacak kurtarma: Dolum olmadı',0,'{}')")
        db.commit()
        db.close()

    def test_export_is_frozen_labelled_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("s", "l"):
                self.make_db(os.path.join(d, name + ".db"))
            self.make_db(os.path.join(d, "o.db"), observer=True)
            out = os.path.join(d, "ds")
            m = v2_dataset.export(out, os.path.join(d, "s.db"), os.path.join(d, "l.db"),
                                  os.path.join(d, "o.db"))
            self.assertEqual(len(m["sha256"]), 64)
            self.assertEqual(m["counts"]["OBSERVER"]["observations"], 1)
            with self.assertRaises(SystemExit):
                v2_dataset.export(out, os.path.join(d, "s.db"), os.path.join(d, "l.db"),
                                  os.path.join(d, "o.db"))
            db = sqlite3.connect(os.path.join(out, "dataset.sqlite3"))
            base = ev.baseline(db)
            db.close()
            # The single-leg UNI fixture is a leg recovery, never a win.
            self.assertEqual(base["trades"][0]["outcome"], "LEG_RECOVERY")
            self.assertEqual(base["totals"]["ALL/ALL"]["wins"], 0)
            self.assertIn("SIMULATED", base["label"])
            json.dumps(base)


if __name__ == "__main__":
    unittest.main()
