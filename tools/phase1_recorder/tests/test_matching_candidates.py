"""matching.Candidates keeps only the snapshots compare() can use; compare over them
must give exactly what it gives over every snapshot. Synthetic (MOCK) series only."""

import random
import unittest

from tools.phase1_recorder import matching

H = 3600
T0 = 1_790_000_000 // H * H


def series(rng):
    """Payments (gaps, sub-second offsets, duplicates, unknown signs) and snapshots
    (ties, missing rates, announced times right and wrong) of one market."""
    hours = rng.randint(0, 40)
    paid = []
    for h in range(hours):
        if rng.random() < 0.15:
            continue  # a period with no recorded payment
        t = T0 + h * H + rng.choice([0, 0, 0.3, -0.4, 1.0, 20])
        rate = rng.choice([0.0001, -0.0002, 0.0, 1e-12, None])
        paid.append(dict(settled_at=t, settled_rate=rate, settled_rate_raw=str(rate),
                         raw_sha256="p%d" % h, revision=rng.choice([1, 1, 2])))
        if rng.random() < 0.1:  # the same payment twice (same whole second)
            paid.append(dict(paid[-1], raw_sha256="dup%d" % h))
    snaps = []
    for k in range(rng.randint(0, 400)):
        r = T0 - H + rng.random() * (hours + 2) * H
        if snaps and rng.random() < 0.05:
            r = snaps[-1]["received_at"]  # an equal time
        nxt = None
        if rng.random() < 0.3:
            nxt = (int(r // H) + 1) * H + rng.choice([0, 0, 30, 61, H])
        snaps.append(dict(
            venue="V", asset="A", market_id=1, received_at=r,
            published_rate=None if rng.random() < 0.05 else rng.choice([0.0001, -0.0001, 0.0]),
            published_rate_raw="x%d" % k, next_funding_at=nxt, raw_sha256="s%d" % k,
        ))
    rng.shuffle(snaps)  # file order is not time order across days
    return paid, snaps


class CandidatesTests(unittest.TestCase):
    def test_compare_over_candidates_equals_compare_over_everything(self):
        rng = random.Random(20261103)
        checked = 0
        for case in range(3000):
            paid, snaps = series(rng)
            hint = rng.choice([H, None])
            now = T0 + 50 * H
            full = matching.compare(snaps, paid, now, 7200, hint)
            c = matching.Candidates(paid, hint)
            for x in snaps:
                c.add(x)
            kept = c.kept()
            self.assertLessEqual(len(kept), len(paid) + 1)
            reduced = matching.compare(kept, paid, now, 7200, hint)
            self.assertEqual(reduced, full, "case %d" % case)
            checked += len(full[0]) + len(full[1])
        self.assertGreater(checked, 1000)  # the cases did produce comparisons

    def test_no_payments_keeps_only_the_latest_snapshot(self):
        c = matching.Candidates([], H)
        for r in (T0 + 5, T0 + 9, T0 + 7):
            c.add(dict(received_at=r, published_rate=0.1))
        self.assertEqual([x["received_at"] for x in c.kept()], [T0 + 9])


if __name__ == "__main__":
    unittest.main()
