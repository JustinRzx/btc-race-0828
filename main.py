"""cbBTC washout/rebound bot for make.fun agent-fun/bars-replay/v1.

Long only, one position at a time, all comparisons in integer arithmetic.
Stage A buys a deep washout only after a short-horizon bounce confirms it,
then rides the rebound with a trailing stop.  Stage B is a shallower range
trade, allowed only if Stage A closed above the starting capital.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
START_CAPITAL = 1_000_000_000
BPS = 10_000

SIZE_BPS = 9_800

A_DIP_BPS = 430
A_BOUNCE_BPS = 50
A_HIGH_BARS = 360
A_LOW_BARS = 30
A_TRAIL_ARM_BPS = 100
A_TRAIL_GAP_BPS = 80
A_STOP_BPS = 120
A_MAX_BARS = 720

B_DIP_BPS = 180
B_BOUNCE_BPS = 30
B_HIGH_BARS = 240
B_LOW_BARS = 30
B_TAKE_BPS = 100
B_STOP_BPS = 120
B_MAX_BARS = 240
B_COOLDOWN_BARS = 30
B_MAX_TRIPS = 2

MAX_SLIPPAGE_BPS = 500


class Strategy:
    def __init__(self):
        self.closes = []
        self.bar = -1
        self.stage = "A"
        self.pending = None
        self.entry_close = None
        self.entry_bar = None
        self.high_since_entry = None
        self.trail_armed = False
        self.b_trips = 0
        self.b_cooldown_until = None
        self.order_no = 0

    def next_id(self, side):
        self.order_no += 1
        return "%s%d" % (side, self.order_no)

    def held_qty(self, state):
        pos = state.positions.get(SYMBOL)
        return 0 if pos is None else pos.qty

    def apply_fills(self, state, batch):
        """Match this order's fill by client_id; only a filled status mutates state.

        Returns True once the order is settled.  An unmatched pending order keeps
        the bot from emitting another order on the same batch.
        """
        order = self.pending
        for fill in batch["fills"]:
            if fill.get("client_id") != order["client_id"]:
                continue
            self.pending = None
            if fill.get("status") != "filled":
                return True
            if order["side"] == "buy":
                self.entry_close = order["ref_close"]
                self.entry_bar = order["decision_bar"]
                self.high_since_entry = int(state.latest[SYMBOL]["c"])
                self.trail_armed = False
            else:
                self.entry_close = None
                self.entry_bar = None
                self.high_since_entry = None
                self.trail_armed = False
                if order["stage"] == "A":
                    if state.equity > START_CAPITAL:
                        self.stage = "B"
                        self.b_trips = 0
                        self.b_cooldown_until = None
                    else:
                        self.stage = "DONE"
                else:
                    self.b_trips += 1
                    self.b_cooldown_until = order["decision_bar"] + B_COOLDOWN_BARS
                    if self.b_trips >= B_MAX_TRIPS:
                        self.stage = "DONE"
            return True
        return self.pending is None

    def on_batch(self, state, batch):
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return None
        close = int(bar["c"])
        self.bar += 1
        self.closes.append(close)

        if self.pending is not None:
            if not self.apply_fills(state, batch):
                return None

        if state.warming_up:
            return None

        held = self.held_qty(state)

        if held > 0:
            if self.entry_close is None:
                return None
            if self.high_since_entry is None or close > self.high_since_entry:
                self.high_since_entry = close

            if self.stage == "A":
                if close * BPS >= self.entry_close * (BPS + A_TRAIL_ARM_BPS):
                    self.trail_armed = True
                hit_trail = self.trail_armed and close * BPS <= self.high_since_entry * (BPS - A_TRAIL_GAP_BPS)
                hit_stop = close * BPS <= self.entry_close * (BPS - A_STOP_BPS)
                hit_time = self.bar - self.entry_bar >= A_MAX_BARS
                if hit_trail or hit_stop or hit_time:
                    return self.sell(state, "A")
            elif self.stage == "B":
                hit_take = close * BPS >= self.entry_close * (BPS + B_TAKE_BPS)
                hit_stop = close * BPS <= self.entry_close * (BPS - B_STOP_BPS)
                hit_time = self.bar - self.entry_bar >= B_MAX_BARS
                if hit_take or hit_stop or hit_time:
                    return self.sell(state, "B")
            return None

        if self.stage == "A":
            if len(self.closes) < A_HIGH_BARS:
                return None
            high = max(self.closes[-A_HIGH_BARS:])
            low = min(self.closes[-A_LOW_BARS:])
            deep = close * BPS <= high * (BPS - A_DIP_BPS)
            bounce = close * BPS >= low * (BPS + A_BOUNCE_BPS)
            if deep and bounce:
                return self.buy(state, "A")
        elif self.stage == "B":
            if len(self.closes) < B_HIGH_BARS:
                return None
            if self.b_cooldown_until is None or self.bar >= self.b_cooldown_until:
                high = max(self.closes[-B_HIGH_BARS:])
                low = min(self.closes[-B_LOW_BARS:])
                deep = close * BPS <= high * (BPS - B_DIP_BPS)
                bounce = close * BPS >= low * (BPS + B_BOUNCE_BPS)
                if deep and bounce:
                    return self.buy(state, "B")
        return None

    def buy(self, state, stage):
        notional = state.equity * SIZE_BPS // BPS
        if notional < 1_000_000:
            return None
        cid = self.next_id("b")
        self.pending = {
            "side": "buy",
            "ref_close": self.closes[-1],
            "decision_bar": self.bar,
            "stage": stage,
            "client_id": cid,
        }
        return [{
            "side": "buy",
            "client_id": cid,
            "symbol": SYMBOL,
            "notional_quote_units": str(notional),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def sell(self, state, stage):
        qty = self.held_qty(state)
        if qty <= 0:
            return None
        cid = self.next_id("s")
        self.pending = {
            "side": "sell",
            "ref_close": self.entry_close,
            "decision_bar": self.bar,
            "stage": stage,
            "client_id": cid,
        }
        return [{
            "side": "sell",
            "client_id": cid,
            "symbol": SYMBOL,
            "qty": str(qty),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]


STRATEGY = Strategy()


def on_batch(state, batch):
    return STRATEGY.on_batch(state, batch)


def main():
    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
