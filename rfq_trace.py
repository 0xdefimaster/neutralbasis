"""RFQ stream allocation trace (PAPER ONLY; observes, never decides).

Records, per desk cycle and in memory only:
  * which cycle's candidate list the RFQ stream allocation used (`ranking_at`),
  * the complete ordered Extended-RFQ candidate list with its evidence, economic and
    waiting state,
  * the allocation decision and why each stream was added or dropped,
  * every deep-check attempt in order: whether it consumed the deep-check quota, why
    it was skipped or rejected, and the RFQ book state at that moment,
and a separate event log of stream lifecycle (subscribe, connected, first snapshot,
gap, loss, retire).

Bounded ring buffers; no SQLite, no disk, no Store.lock. Only whitelisted scalar
fields are kept (strings truncated); WebSocket payloads, raw API bodies and
credentials are never recorded. With `enabled=False` every call is a no-op.
"""

import collections, itertools, threading, time

# Measured (2026-11-09, recorded candidate lists): ~33 RFQ candidates and ~33 deep-check
# rows per cycle. 120 cycles cover ~6 h at the observed cycle pace; the external
# recorder reads every minute, so the ring only has to bridge short gaps.
CYCLES_KEEP = 120
EVENTS_KEEP = 5000
TEXT_MAX = 160
PAGE_CYCLES_MAX = 20
PAGE_EVENTS_MAX = 1000
RESPONSE_BYTES_MAX = 1_000_000

EVENT_KINDS = ("subscribe", "connected", "first_snapshot", "gap", "lost", "retire")
EVENT_FIELDS = ("seq", "exchange_ts", "reason", "error", "was_ready", "cycle_id", "market")


def _text(v):
    if v is None:
        return None
    return str(v)[:TEXT_MAX]


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _texts(xs, limit):
    """A short tuple of short strings (the empty tuple is shared, so it costs nothing)."""
    return tuple(_text(x) for x in list(xs or ())[:limit])


class RfqTrace:
    def __init__(self, enabled=True, cycles_keep=CYCLES_KEEP, events_keep=EVENTS_KEEP,
                 clock=time.time):
        self.enabled = enabled
        self.clock = clock
        self.lock = threading.Lock()
        self.cycles = collections.deque(maxlen=cycles_keep)
        self.events = collections.deque(maxlen=events_keep)
        self._cycle_ids = itertools.count(1)
        self._event_ids = itertools.count(1)
        self.current = None
        # Measured cost of the trace calls themselves (seconds), per finished cycle.
        self.overhead = collections.deque(maxlen=cycles_keep)
        self._spent = 0.0
        # Failures inside observation code; they are counted, never raised into the
        # decision path.
        self.errors = 0
        self.last_error = None

    def guard(self, fn, *args, **kwargs):
        """Run an observation call; any exception is counted and swallowed so the
        trace can never change or stop a decision."""
        if not self.enabled:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as ex:  # observation only
            self.errors += 1
            self.last_error = _text("%s: %s" % (type(ex).__name__, ex))
            return None

    # ---- cycle records ---------------------------------------------------------
    def begin_cycle(self, started_at=None):
        if not self.enabled:
            return
        t0 = time.perf_counter()
        with self.lock:
            self.current = dict(
                cycle_id=next(self._cycle_ids),
                started_at=started_at if started_at is not None else self.clock(),
                ranking_at=None, deep_ranking_at=None, deep_top=None, rfq_candidates=[],
                allocation=None, deep_checks=[], streams=None, ended_at=None,
            )
            self._spent = time.perf_counter() - t0

    def cycle_id(self):
        c = self.current
        return c["cycle_id"] if c else None

    def note_allocation(self, ranking_at, candidates, ranked, held, since, chosen, at,
                        cap, eligible=None, hold=None):
        """`candidates`: [(strategy, rank, candidate dict, waiting dict or None,
        blocked reasons, research codes)] for Extended-RFQ pairs, in ranking order.
        `ranked`: the assets in the priority order the allocation used, with the
        key ((waiting or research), -score). `since`: start time of running streams."""
        if not self.enabled or self.current is None:
            return
        t0 = time.perf_counter()
        rows = []
        for strategy, rank, c, waiting, blocked, codes in candidates:
            rows.append((
                _text(strategy), rank, _text(c.get("asset")), _text(c.get("long")),
                _text(c.get("short")), _num(c.get("score")), _num(c.get("screen_score")),
                bool((_num(c.get("score")) or 0) > 0), _texts(codes, 12),
                _num((waiting or {}).get("until")), _text((waiting or {}).get("group")),
                _texts(blocked, 6),
                "long" if c.get("rfq_long") else "short",
            ))
        chosen = set(chosen)
        prev = set(since)
        rank_order = [a for a, _ in ranked]
        # What the allocation would have chosen without the young-stream hold
        # (held legs first, then the ranked order up to the cap): a chosen stream
        # outside this set is there only because it is young.
        by_rank = set(held)
        for a in rank_order:
            if len(by_rank) >= max(cap, len(held)):
                break
            by_rank.add(a)

        def why(a):
            return "held" if a in held else ("ranked" if a in by_rank else "young_kept")

        added = {a: why(a) for a in chosen - prev}
        kept = {a: why(a) for a in chosen & prev}
        dropped = {a: ("not_ranked" if a not in rank_order else "cap")
                   for a in prev - chosen}
        alloc = dict(
            at=at, cap=cap, hold=_num(hold),
            eligible=None if eligible is None else sorted(_text(a) for a in eligible),
            ranked=[(_text(a), [bool(k[0]), _num(k[1])]) for a, k in ranked],
            held=sorted(_text(a) for a in held),
            since={_text(a): _num(t) for a, t in since.items()},
            chosen=sorted(_text(a) for a in chosen),
            added={_text(a): r for a, r in added.items()},
            kept={_text(a): r for a, r in kept.items()},
            dropped={_text(a): r for a, r in dropped.items()},
        )
        with self.lock:
            if self.current is not None:
                self.current["ranking_at"] = _num(ranking_at)
                self.current["rfq_candidates"] = rows
                self.current["allocation"] = alloc
                self._spent += time.perf_counter() - t0
        for a, r in added.items():
            self.event("subscribe", a, reason=r)
        for a, r in dropped.items():
            self.event("retire", a, reason=r)

    def note_deep_ranking(self, at, top=None):
        if not self.enabled or self.current is None:
            return
        with self.lock:
            if self.current is not None:
                self.current["deep_ranking_at"] = _num(at)
                self.current["deep_top"] = _num(top)

    def note_deep(self, strategy, order, c, action, slot_consumed, rfq_book,
                  outcome=()):
        """One deep-check attempt, in the order the round made it."""
        if not self.enabled or self.current is None:
            return
        t0 = time.perf_counter()
        row = (
            _text(strategy), order, _text(c.get("asset")), _text(c.get("long")),
            _text(c.get("short")), _text(action), bool(slot_consumed),
            _clean_book(rfq_book), _texts(outcome, 12),
        )
        with self.lock:
            if self.current is not None:
                self.current["deep_checks"].append(row)
                self._spent += time.perf_counter() - t0

    def end_cycle(self, streams=None):
        if not self.enabled or self.current is None:
            return
        t0 = time.perf_counter()
        with self.lock:
            c, self.current = self.current, None
            if c is None:
                return
            c["streams"] = {
                _text(a): _clean_book(s) for a, s in (streams or {}).items()
            }
            c["ended_at"] = self.clock()
            spent = self._spent + (time.perf_counter() - t0)
            c["trace_seconds"] = spent
            self.overhead.append(spent)
            self.cycles.append(c)

    # ---- stream events ---------------------------------------------------------
    def event(self, kind, asset, at=None, **fields):
        if not self.enabled or kind not in EVENT_KINDS:
            return
        e = dict(kind=kind, asset=_text(asset), at=at if at is not None else self.clock())
        for k in EVENT_FIELDS:
            if k in fields:
                v = fields[k]
                e[k] = v if isinstance(v, (bool, int, float)) or v is None else _text(v)
        c = self.current
        if "cycle_id" not in e and c is not None:
            e["cycle_id"] = c["cycle_id"]
        with self.lock:
            e["id"] = next(self._event_ids)
            self.events.append(e)

    # ---- read side (HTTP) ------------------------------------------------------
    def page(self, since_cycle=0, since_event=0, cycles=PAGE_CYCLES_MAX,
             events=PAGE_EVENTS_MAX, max_bytes=RESPONSE_BYTES_MAX):
        """Cycles with id > since_cycle and events with id > since_event, oldest first,
        bounded in count and in serialized size."""
        import json

        cycles = max(1, min(int(cycles), PAGE_CYCLES_MAX))
        events = max(1, min(int(events), PAGE_EVENTS_MAX))
        with self.lock:
            cs = [c for c in self.cycles if c["cycle_id"] > since_cycle][:cycles]
            es = [e for e in self.events if e["id"] > since_event][:events]
        out = dict(mode="PAPER", enabled=self.enabled, cycles=cs, events=es,
                   truncated=False, kept=dict(cycles=self.cycles.maxlen,
                                              events=self.events.maxlen),
                   trace_errors=self.errors, last_trace_error=self.last_error,
                   cost_seconds=self.cost(), book_fields=BOOK_FIELDS,
                   schema=dict(
                       rfq_candidates=("strategy", "rank", "asset", "long", "short",
                                       "score", "screen_score", "economic_positive",
                                       "research_codes", "waiting_until", "waiting_group",
                                       "blocked", "rfq_leg"),
                       deep_checks=("strategy", "order", "asset", "long", "short",
                                    "action", "slot_consumed", "book", "outcome"),
                   ))
        def finish():
            out["next_cycle"] = out["cycles"][-1]["cycle_id"] if out["cycles"] else since_cycle
            out["next_event"] = out["events"][-1]["id"] if out["events"] else since_event
            # The response is sent as UTF-8 bytes; measure exactly that.
            return len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode("utf8"))

        size = finish()
        while size > max_bytes and (out["cycles"] or out["events"]):
            out["truncated"] = True
            if len(out["cycles"]) >= 1 and (len(out["cycles"]) > 1 or not out["events"]):
                out["cycles"] = out["cycles"][:-1]
            else:
                out["events"] = out["events"][: len(out["events"]) // 2]
            size = finish()
        return out

    def cost(self):
        xs = sorted(self.overhead)
        if not xs:
            return dict(n=0, p50=None, p95=None, max=None)
        pick = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
        return dict(n=len(xs), p50=pick(0.5), p95=pick(0.95), max=xs[-1])


# Stored book states are compact tuples in this field order (trailing unknowns are
# dropped); `connected`/`ready` are implied by `state`.
BOOK_FIELDS = ("state", "seq", "age", "gaps", "reconnects", "snapshots",
               "subscribed_at", "first_snapshot_at", "alive_age")


def _scalar(v):
    return v if isinstance(v, (bool, int, float)) or v is None else _text(v)


def _clean_book(s):
    if s is None:
        return None
    if isinstance(s, str):
        return (_text(s),)
    out = [_scalar(s.get(k)) for k in BOOK_FIELDS]
    while out and out[-1] is None:
        out.pop()
    return tuple(out)


def book_state(status):
    """A stream status dict (RfqStream.status) -> compact book state for the trace."""
    if status is None:
        return dict(state="not_subscribed")
    if not status.get("connected"):
        state = "connecting"
    elif not status.get("ready"):
        state = "warming"
    else:
        import extended_rfq

        # Same rule as RfqStream.view: a live link (any frame) and a recent data frame.
        age = status.get("age")
        alive = status.get("alive_age")
        alive = age if alive is None else (alive if age is None else min(age, alive))
        state = (
            "stale"
            if age is None
            or age > extended_rfq.VIEW_MAX_AGE
            or alive > extended_rfq.ALIVE_MAX_AGE
            else "ready"
        )
    extra = ("connected", "ready") + BOOK_FIELDS[1:]
    return dict(state=state, **{k: status.get(k) for k in extra if k in status})
