"""Funding payments of a PAPER position: realized only with the venue's own settlement
price, otherwise an estimate; distinct payments never merged.

A public settled row gives a rate per payment and its time. The cash of a payment is
units x settlement price x rate, signed (positive rate: longs pay, shorts receive).
The settlement price basis is the venue's (docs read 2026-11-12):

  lighter_rh   index price           (Lighter funding docs: position x index x rate)
  risex        index price at settlement time (history field `index_price`); checked
               on live data: accumulated_funding steps by rate x index_price (99/99)
  arcus        oracle price          (docs: payment = rate x size x oracle price)
  tradexyz     oracle price          (Hyperliquid funding docs, "not the mark price")
  txflow       mark price            (no public settled history at all)
  variational  index price           (no public settled history at all)

Only a row that carries that price (`price`, as the venue published it at the payment)
is booked as `funding` (realized). Valuing at the entry price is an ESTIMATE and is
booked as `funding_estimated`, kept out of the realized result.

Identity of a payment: (leg, time, rate). The same row seen again is booked once; two
different rates at the same second are two payments (never merged).
"""
import hashlib

PRICE_BASIS = dict(lighter_rh="index", risex="index", arcus="oracle", tradexyz="oracle",
                   txflow="mark", variational="index")


def payment_key(side, ts, rate):
    h = hashlib.sha1(repr(float(rate)).encode()).hexdigest()[:10]
    return "funding:%s:%d:%s" % (side, int(ts), h)


def payment_event(side, units, entry_price, row, exchange_id=None):
    """(key, kind, amount, info) for one settled row."""
    sign = -1 if side == "long" else 1
    rate = float(row["rate"])
    price = row.get("price")
    if price is not None:
        kind, valuation = "funding", "venue settlement price (%s)" % (
            row.get("price_basis") or PRICE_BASIS.get(exchange_id, "?"))
    else:
        kind, valuation, price = "funding_estimated", "entry price (estimate)", entry_price
    return (payment_key(side, row["ts"], rate), kind, sign * units * price * rate,
            dict(valuation=valuation, ts=row["ts"], rate=rate))
