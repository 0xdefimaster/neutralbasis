"""Normalized paper records: periods, trades and an append-only ledger of cash events.

The paper desk keeps its working state in kv JSON; these tables mirror it in a form an
auditor can query. Ledger events are only ever inserted, each under a unique key, so
a repeated write never doubles a cash flow:

  payment:<payment key>          funding cash of one settled payment (0 if unverified)
  payment:<payment key>:late     the cash a late-published rate added to a zero
  trade:<id>:price               a closed trade's realized price PnL
  trade:<id>:fees                its fees (negative)

Each funding payment carries a status (`payment_status`):

  SETTLED                  published rate, observed price: realized cash
  SETTLED_ESTIMATED_PRICE  published rate, price estimated (the entry fill): an
                           estimate, not realized
  NOT_RECORDED             the venue published no rate after the wait
  UNKNOWN                  the venue's history could not be read

NOT_RECORDED and UNKNOWN are written with no amount (null), never as a realized zero.
A closed trade reconciles when its SETTLED events (price, fees and settled funding)
sum to its realized result: net minus its estimated funding.
"""

import json


def period_values(e, source):
    return (
        e.get("started"),
        e.get("ended"),
        e.get("capital"),
        json.dumps(e.get("venues")),
        json.dumps(e.get("workspace")),
        json.dumps(e.get("config")),
        e.get("trades"),
        e.get("net"),
        source,
    )


def trade_row(p, period_id):
    legs = p.get("legs") or []
    side = {leg.get("side"): leg.get("venue") for leg in legs}
    body = {k: v for k, v in p.items() if k not in ("legs", "decision")}
    return (
        p["id"],
        period_id,
        p.get("strategy", "funding"),
        p.get("asset"),
        side.get("long", p.get("long")),
        side.get("short", p.get("short")),
        p.get("opened"),
        p.get("closed_at"),
        (p.get("decision") or {}).get("size", p.get("size")),
        p.get("price_pnl", 0.0),
        p.get("funding", 0.0),
        p.get("fees", 0.0),
        p.get("net", 0.0),
        p.get("close_reason", p.get("reason")),
        int(bool(p.get("pending_funding", p.get("pending")))),
        json.dumps(body, default=str),
    )


def trade_events(p):
    """The price and fee events of a closed trade (funding comes from its payments)."""
    at = p.get("closed_at")
    return [
        (
            "trade:%s:price" % p["id"],
            at,
            "price_pnl",
            p["id"],
            None,
            p.get("price_pnl", 0.0),
            None,
        ),
        (
            "trade:%s:fees" % p["id"],
            at,
            "fees",
            p["id"],
            None,
            -p.get("fees", 0.0),
            None,
        ),
    ]


SETTLED = "SETTLED"
SETTLED_ESTIMATED_PRICE = "SETTLED_ESTIMATED_PRICE"
NOT_RECORDED = "NOT_RECORDED"
UNKNOWN = "UNKNOWN"
UNSETTLED = (NOT_RECORDED, UNKNOWN)
KIND = {
    SETTLED: "funding",
    SETTLED_ESTIMATED_PRICE: "funding_estimated",
    NOT_RECORDED: "funding_not_recorded",
    UNKNOWN: "funding_unknown",
}


def payment_status(body):
    """A payment's status; records written before statuses existed are read from
    their flags (unverified zero, estimated valuation)."""
    if body.get("status"):
        return body["status"]
    if body.get("unverified"):
        return NOT_RECORDED
    if body.get("valuation_estimated"):
        return SETTLED_ESTIMATED_PRICE
    return SETTLED


def payment_event(key, ts, body, late=False, suffix=None):
    status = payment_status(body)
    kind = KIND[status] + ("_late" if late and status not in UNSETTLED else "")
    return (
        "payment:%s%s" % (key, suffix or (":late" if late else "")),
        ts,
        kind,
        body.get("position"),
        body.get("venue"),
        None if status in UNSETTLED else (body.get("cash", 0.0) or 0.0),
        json.dumps(dict(body, status=status), default=str),
    )


class Ledger:
    """Writes to the normalized tables (created by migration 1); a store without them
    (an older database before migration) is left alone."""

    def __init__(self, store):
        self.store = store

    def ready(self):
        with self.store.lock:
            return bool(
                self.store.db.execute(
                    "select 1 from sqlite_master where type='table' and name='ledger_events'"
                ).fetchone()
            )

    def current_period(self):
        with self.store.lock:
            r = self.store.db.execute(
                "select id from paper_periods where ended is null order by id desc limit 1"
            ).fetchone()
        return r[0] if r else None

    def new_period(self, archived, started, wallet, workspace, config, at):
        """Close the running period row with its archived summary and open the next."""
        if not self.ready():
            return
        with self.store.lock:
            db = self.store.db
            pid = self.current_period()
            if pid is not None:
                db.execute(
                    "update paper_periods set ended=?, capital=?, venues=?, workspace=?,"
                    " config=?, trades=?, net=?, source=? where id=?",
                    (
                        archived.get("ended"),
                        archived.get("capital"),
                        json.dumps(archived.get("venues")),
                        json.dumps(archived.get("workspace")),
                        json.dumps(archived.get("config")),
                        archived.get("trades"),
                        archived.get("net"),
                        "arşiv",
                        pid,
                    ),
                )
            db.execute(
                "insert into paper_periods(started, ended, capital, venues, workspace,"
                " config, trades, net, source) values(?,?,?,?,?,?,?,?,?)",
                (
                    started,
                    None,
                    config.get("wide_capital"),
                    json.dumps(wallet.get("venues")),
                    json.dumps(workspace),
                    json.dumps(config),
                    None,
                    None,
                    "açık dönem",
                ),
            )
            db.commit()

    def set_workspace(self, workspace, config):
        if not self.ready():
            return
        with self.store.lock:
            pid = self.current_period()
            if pid is not None:
                self.store.db.execute(
                    "update paper_periods set workspace=?, config=? where id=?",
                    (json.dumps(workspace), json.dumps(config), pid),
                )
                self.store.db.commit()

    def record_trade(self, p):
        """Mirror a closed trade (updated while late funding settles) and append its
        price and fee events once."""
        if not self.ready() or not p.get("id") or not p.get("legs"):
            return
        with self.store.lock:
            db = self.store.db
            db.execute(
                "insert or replace into paper_trades values"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                trade_row(p, self.current_period()),
            )
            for row in trade_events(p):
                db.execute(
                    "insert or ignore into ledger_events(event_key, ts, kind, position,"
                    " venue, amount, body) values(?,?,?,?,?,?,?)",
                    row,
                )
            db.commit()

    def record_payment(self, key, ts, body, late=False, suffix=None):
        """Append the event of a payment: the cash of a settled one (late: the cash
        that replaced an unsettled one), or an unsettled one with no amount."""
        if not self.ready():
            return
        with self.store.lock:
            self.store.db.execute(
                "insert or ignore into ledger_events(event_key, ts, kind, position,"
                " venue, amount, body) values(?,?,?,?,?,?,?)",
                payment_event(key, ts, body, late, suffix),
            )

    def reconcile(self, period_id=None):
        """Closed trades of a period whose SETTLED events do not sum to their realized
        result (net minus estimated funding): [(trade id, settled sum, realized)].
        Estimated and unsettled payments never enter it."""
        with self.store.lock:
            db = self.store.db
            pid = self.current_period() if period_id is None else period_id
            trades = db.execute(
                "select id, net, body from paper_trades where period_id=?"
                " and body not like '%\"ledger\": true%'",
                (pid,),
            ).fetchall()
            events = {}
            for position, kind, amount, body in db.execute(
                "select position, kind, amount, body from ledger_events"
                " where position in (select id from paper_trades where period_id=?)",
                (pid,),
            ):
                events.setdefault(position, []).append((kind, amount, body))
        out = []
        for tid, net, body in trades:
            realized = (net or 0.0) - (json.loads(body).get("estimated_funding") or 0.0)
            total = 0.0
            for kind, amount, ebody in events.get(tid, []):
                if amount is None:
                    continue
                if (
                    kind.startswith("funding")
                    and payment_status(json.loads(ebody or "{}")) != SETTLED
                ):
                    continue
                total += amount
            if abs(total - realized) > 1e-6:
                out.append((tid, total, realized))
        return out
