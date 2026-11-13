"""Variational adapter v2: adaptive polling and five-level PnL (Phase 13.3). PAPER.

One public endpoint (GET /metadata/v2/stats) returns every listing with funding,
interval, next funding time, mark, indicative bid/ask per notional tier and
quotes.updated_at (docs.variational.io/technical-documentation/api, read 2026-11-13).
Docs: quotes "may be cached for up to 600 seconds"; 10 requests / 10 s per IP and
1000 / min global; 429 carries `retry_after_seconds`; no WebSocket; the trading API is
"not yet available to any users".

AdaptivePoller (pure scheduling, no I/O):
  * starts at 30 s, stays within [30 s, 60 s];
  * learns the real refresh cadence from the distinct `updated_at` values it sees and
    polls at about that cadence (median refresh, clamped);
  * when an answer repeats the previous quotes (cached at the venue) the interval
    grows by 1.5x (up to 60 s);
  * after HTTP 429 nothing is sent before `retry_after_seconds` (or 10 s if absent);
  * single-flight: a second request is refused while one is in flight;
  * one request serves every listing (all markets come in one answer).

pnl_levels(): the five levels of a Variational-vs-other-venue price gap, kept apart:
  1 THEORETICAL         last observed mids (Variational mid may be old)
  2 AGE_ADJUSTED        level 1 with a band of +-1 sigma over the quote's age
  3 NET_AFTER_COSTS     executable sides (buy at ask / sell at bid of the tier), fees
  4 RFQ_STRESS          level 3 minus a firm-quote gap and the staleness move
  5 EXECUTABLE_PROOF    always NOT_VERIFIED without a firm quote (no trading API)
"""
import math
import statistics
import threading
import time

MIN_S, MAX_S, START_S = 30.0, 60.0, 30.0


def json_key(x):
    import json

    return None if x is None else json.dumps(x, sort_keys=True, default=str)
DEFAULT_RETRY_S = 10.0


class AdaptivePoller:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.interval = START_S
        self.next_at = 0.0
        self.last_quotes = None
        self.per_listing = {}           # ticker -> distinct updated_at values (epoch s)
        self.in_flight = False
        self.lock = threading.Lock()
        self.stats = dict(requests=0, repeats=0, http_429=0, refused_in_flight=0)

    def due(self):
        return self.clock() >= self.next_at and not self.in_flight

    def acquire(self):
        """True if a request may be sent now (single-flight and schedule)."""
        with self.lock:
            if self.in_flight:
                self.stats["refused_in_flight"] += 1
                return False
            if self.clock() < self.next_at:
                return False
            self.in_flight = True
            self.stats["requests"] += 1
            return True

    def on_answer(self, listings):
        """listings: {ticker: updated_at epoch s (or None when the quote is missing)}
        or {ticker: dict(updated_at=..., funding=<any comparable funding state>)}.

        Cadence is learned PER LISTING (each listing's own distinct updated_at values;
        never one merged list, which would make the venue look faster than it is), and
        the request interval is the median of the listings' median refresh, clamped to
        [30, 60] s. Only differences of the venue's own timestamps are used, so a local
        clock offset does not bias it. The answer is a cached REPEAT only if every
        listing's quote time AND funding state are unchanged."""
        with self.lock:
            self.in_flight = False
            norm = {}
            for tk, v in (listings or {}).items():
                if isinstance(v, dict):
                    norm[tk] = (v.get("updated_at"), json_key(v.get("funding")))
                else:
                    norm[tk] = (v, None)
            key = tuple(sorted(norm.items(), key=lambda kv: kv[0]))
            if key == self.last_quotes:
                self.stats["repeats"] += 1
                self.interval = min(MAX_S, self.interval * 1.5)
            else:
                for tk, (u, _) in norm.items():
                    if u is None:
                        continue                     # missing quote: no cadence sample
                    s = self.per_listing.setdefault(tk, [])
                    if not s or u > s[-1]:
                        s.append(u)
                    del s[:-50]
                meds = []
                for s in self.per_listing.values():
                    gaps = [b - a for a, b in zip(s, s[1:]) if b > a]
                    if len(gaps) >= 3:
                        meds.append(statistics.median(gaps))
                if meds:
                    self.interval = min(MAX_S, max(MIN_S, statistics.median(meds)))
            self.last_quotes = key
            self.next_at = self.clock() + self.interval

    def cadence(self):
        """{ticker: median refresh s} from each listing's own updated_at history."""
        out = {}
        for tk, s in self.per_listing.items():
            gaps = [b - a for a, b in zip(s, s[1:]) if b > a]
            out[tk] = statistics.median(gaps) if gaps else None
        return out

    def on_429(self, retry_after=None):
        with self.lock:
            self.in_flight = False
            self.stats["http_429"] += 1
            wait = retry_after if retry_after and retry_after > 0 else DEFAULT_RETRY_S
            self.next_at = self.clock() + max(wait, self.interval)

    def on_error(self):
        with self.lock:
            self.in_flight = False
            self.next_at = self.clock() + self.interval


def pnl_levels(var_quote, other_book, var_is_long, size_usd, quote_age_s,
               sigma_hourly=None, fee_var=0.0, fee_other=0.0, firm_gap_bp=10.0):
    """var_quote: dict(bid, ask) of the Variational tier for `size_usd` (exact tier);
    other_book: dict(bids, asks). Returns the five levels; nothing is verified."""
    vb, va = var_quote["bid"], var_quote["ask"]
    vm = (vb + va) / 2
    ob, oa = other_book["bids"][0][0], other_book["asks"][0][0]
    om = (ob + oa) / 2
    # long Variational / short other: gain if other trades above Variational
    sign = 1.0 if var_is_long else -1.0
    l1 = sign * (om - vm) / vm * size_usd
    move = None if sigma_hourly is None or quote_age_s is None else \
        sigma_hourly * math.sqrt(max(quote_age_s, 0.0) / 3600.0) * size_usd
    l2 = None if move is None else (l1 - move, l1 + move)
    if var_is_long:
        exec_gap = (ob - va) / va * size_usd       # buy Variational ask, sell other bid
    else:
        exec_gap = (vb - oa) / oa * size_usd       # sell Variational bid, buy other ask
    fees = size_usd * (fee_var + fee_other)
    l3 = exec_gap - fees
    l4 = l3 - firm_gap_bp * 1e-4 * size_usd - (move or 0.0)
    return dict(
        THEORETICAL=l1, AGE_ADJUSTED=l2, AGE_BASIS="unknown volatility" if move is None
        else "+-1 sigma over %.0f s" % quote_age_s,
        NET_AFTER_COSTS=l3, RFQ_STRESS=l4,
        EXECUTABLE_PROOF="NOT_VERIFIED: indicative quote only; no firm quote; trading API "
                         "not available",
        label="ESTIMATED (opening entry only; exit costs priced separately)")
