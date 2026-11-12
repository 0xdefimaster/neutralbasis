"""V2 strategies: funding arbitrage, price convergence and their hybrid, each priced by
v2.economics. Every reason carries a category: DATA (an input is missing or not
proven), EXECUTION (the venue cannot be simulated or the book cannot fill), COST (the
inputs are fine but the trade does not pay)."""
DATA, EXECUTION, COST = "DATA", "EXECUTION", "COST"


def reason(category, code, text=""):
    return dict(category=category, code=code, text=text or code)
