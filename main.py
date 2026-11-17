"""Confirmed-bottom dip buyer for the make.fun cbBTC bars race.

Entry for btc-race-24h-20261117-0603 (agent-fun/bars-replay/v1). This is
a faithful reimplementation of the v5 design that was built for the
2026-11-13 race: same zones, same parameters, same exits. Its behaviour
was verified order-for-order identical to the original bot's on this
edition's dev window and on eight real Binance BTC days before it was
submitted (see README).

The idea, in one paragraph: never buy a dip that is still falling. A
close has to sit at least 50 bps under its trailing 45-bar high, a
momentum gauge has to say the fall is tiring (a bullish RSI divergence
at swing lows, or RSI crossing back up through 30), and the price has
to be at a floor that means something — the capitulation low itself, a
retest of an old 240-bar low, or a double bottom against the day's
lowest close. Entries are all-in, at most three per day, and every
position leaves on a fixed take-profit / stop-loss pair measured from
its fee-inclusive cost basis.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"
BPS = 10000

# --- shared gates ------------------------------------------------------
DIP_BPS = 50           # close must be at least this far under the high
HIGH_WIN = 45          # bars in the trailing-high window
RSI_LEN = 14
RSI_OS = 30            # oversold line for the cross-up signal
CROSS_VALID = 10       # bars a cross-up stays usable
DIV_CONFIRM = 5        # bars after the swing low before a divergence counts
DIV_VALID = 30         # bars a divergence stays usable, from the swing low
DIV_BOUNCE_BPS = 30    # smallest real bounce between the two swing lows
SWING_SPAN = 4         # bars on either side of a swing low
LONG_WIN = 240         # window the zones are measured on
LATE_CUTOFF = 1250     # no entries after this bar
MAX_TRIPS = 3          # entries allowed per TRIP_WIN bars
TRIP_WIN = 1440
COOLDOWN = 5           # bars to sit out after an exit
MIN_BARS = 60          # history required before the first entry
SIZE_PCT = 100         # percent of cash per primary entry
BUY_SLIP_BPS = 150     # a fill far above the close means the dip bounced
SELL_SLIP_BPS = 2000   # an exit must never be refused when it matters
TP_BPS = 150           # take profit vs cost basis (capitulation / retest)
SL_BPS = 100           # stop loss vs cost basis (capitulation / retest)

# --- capitulation zone ---------------------------------------------------
CAP_FALL_BPS = 340     # fall from the long high, at least
CAP_BAND_BPS = 40      # close within this of the long low (printing it)
CAP_RSI_MAX = 28       # RSI no higher than this

# --- retest zone ---------------------------------------------------------
RETEST_AGE = 60        # the long low is at least this many bars old
RETEST_BAND_BPS = 170  # close back within this above the long low

# --- day-low retest zone ---------------------------------------------------
DAY_AGE = 240          # the whole window's lowest close is this old
DAY_BAND_BPS = 50      # close back within this above that low
DAY_TP_BPS = 90        # the bounce off a day low is shorter: bank less
DAY_SL_BPS = 100

# --- insurance fallback ----------------------------------------------------
FB_FROM = 1150         # first bar the fallback may fire
FB_CUTOFF = 1400       # last bar it may fire
FB_DIP_BPS = 50        # a modest dip is enough here
FB_RSI_MAX = 45
FB_SIZE_PCT = 20
FB_TP_BPS = 80
FB_SL_BPS = 50


class DipBuyer:
    """One bot instance per hello; feed it batches, it answers orders."""

    def __init__(self, hello):
        sym = hello["symbols"][0]
        self.qty_scale = sym["qty_scale"]
        self.price_scale = sym["price_scale"]
        self.quote_scale = hello["capital"]["quote_scale"]
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.closes = []        # every close seen, in order
        self.rsi = []           # RSI per bar, None until the seed fills
        self._seed_g = 0.0      # RSI seed accumulators (first RSI_LEN changes)
        self._seed_l = 0.0
        self._avg_g = None      # Wilder-smoothed averages once seeded
        self._avg_l = None
        self.swings = []        # (bar, close, rsi) confirmed swing lows
        self.div_lo = 1 << 60   # first bar a divergence may be traded
        self.div_hi = -1        # last bar a divergence may be traded
        self.cross_hi = -1      # last bar a cross-up may be traded
        self.trip_bars = []     # bar index of each filled primary buy
        self.awaiting = None    # "buy"/"sell" while an order is unresolved
        self.cool_to = -1       # first bar a new entry is allowed again
        self.seq = 0            # order counter for client ids
        self.n_buys = 0         # filled buys of any kind
        self.fb_open = False    # the open position is the insurance trade
        self.fb_sent = False    # the unresolved order is the insurance one
        self.zone_sent = None   # zone that sent the unresolved primary buy
        self.zone_open = None   # zone the open primary position came from

    # -- indicators ------------------------------------------------------
    def _feed(self, close):
        """Append one close and refresh RSI, cross-ups and swing lows."""
        self.closes.append(close)
        i = len(self.closes) - 1
        if i == 0:
            self.rsi.append(None)
            return
        delta = self.closes[i] - self.closes[i - 1]
        gain = float(delta) if delta > 0 else 0.0
        loss = float(-delta) if delta < 0 else 0.0
        if i <= RSI_LEN:
            self._seed_g += gain
            self._seed_l += loss
            if i < RSI_LEN:
                self.rsi.append(None)
                return
            self._avg_g = self._seed_g / RSI_LEN
            self._avg_l = self._seed_l / RSI_LEN
        else:
            self._avg_g = (self._avg_g * (RSI_LEN - 1) + gain) / RSI_LEN
            self._avg_l = (self._avg_l * (RSI_LEN - 1) + loss) / RSI_LEN
        if self._avg_l == 0.0:
            self.rsi.append(100.0 if self._avg_g > 0 else 50.0)
        else:
            rs = self._avg_g / self._avg_l
            self.rsi.append(100.0 - 100.0 / (1.0 + rs))
        before = self.rsi[i - 1]
        if before is not None and before <= RSI_OS < self.rsi[i]:
            self.cross_hi = i + CROSS_VALID - 1
        self._scan_swing(i)

    def _scan_swing(self, i):
        """Confirm the swing low SWING_SPAN bars back; spot divergences."""
        s = i - SWING_SPAN
        if s < SWING_SPAN or self.rsi[s] is None:
            return
        c = self.closes[s]
        if not (c < min(self.closes[s - SWING_SPAN:s])
                and c <= min(self.closes[s + 1:s + SWING_SPAN + 1])):
            return
        if self.swings and self.swings[-1][0] == s:
            return
        prev = self.swings[-1] if self.swings else None
        self.swings.append((s, c, self.rsi[s]))
        if prev is None or not (c < prev[1] and self.rsi[s] > prev[2]):
            return
        # Bullish divergence: lower price low, higher RSI low. The two
        # lows need a real bounce between them, not a grind's micro-bounce.
        peak = max(self.closes[prev[0]:s + 1])
        if peak * BPS >= c * (BPS + DIV_BOUNCE_BPS):
            self.div_lo = s + DIV_CONFIRM
            self.div_hi = s + DIV_VALID - 1

    # -- bookkeeping -------------------------------------------------------
    def _basis(self, pos):
        """Price level at which the position's value equals its cost."""
        return (pos.cost * 10 ** (self.qty_scale + self.price_scale)
                // (pos.qty * 10 ** self.quote_scale))

    def _cid(self, tag):
        self.seq += 1
        return "%s%d" % (tag, self.seq)

    def _absorb_fills(self, fills):
        """Fold the resolutions of the orders sent on earlier batches."""
        for f in fills:
            if self.awaiting == "buy" and f["side"] == "buy":
                if f.get("status") == "filled":
                    self.n_buys += 1
                    if self.fb_sent:
                        self.fb_open = True
                    else:
                        self.trip_bars.append(len(self.closes) - 1)
                        self.zone_open = self.zone_sent
                self.awaiting = None
                self.fb_sent = False
                self.zone_sent = None
            elif self.awaiting == "sell" and f["side"] == "sell":
                if f.get("status") == "filled":
                    self.cool_to = len(self.closes) - 1 + COOLDOWN
                    if self.fb_sent:
                        self.fb_open = False
                    else:
                        self.zone_open = None
                self.awaiting = None
                self.fb_sent = False

    # -- exits ---------------------------------------------------------------
    def _exit_order(self, pos):
        basis = self._basis(pos)
        if self.fb_open:
            tp_bps, sl_bps = FB_TP_BPS, FB_SL_BPS
        elif self.zone_open == "day":
            tp_bps, sl_bps = DAY_TP_BPS, DAY_SL_BPS
        else:
            tp_bps, sl_bps = TP_BPS, SL_BPS
        close = self.closes[-1]
        tp = basis * (BPS + tp_bps) // BPS
        sl = basis * (BPS - sl_bps) // BPS
        if close >= tp or close <= sl:
            self.awaiting = "sell"
            self.fb_sent = self.fb_open
            return [{"side": "sell", "client_id": self._cid("s"),
                     "symbol": SYMBOL, "qty": pos.qty,
                     "max_slippage_bps": SELL_SLIP_BPS}]
        return None

    # -- entries ---------------------------------------------------------------
    def _buy_order(self, state, pct, zone, fb):
        notional = state.cash * pct // 100
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < self.min_notional:
            return None
        self.awaiting = "buy"
        self.fb_sent = fb
        self.zone_sent = zone
        return [{"side": "buy", "client_id": self._cid("f" if fb else "b"),
                 "symbol": SYMBOL, "notional_quote_units": notional,
                 "max_slippage_bps": BUY_SLIP_BPS}]

    def _dipped(self, i, close, need_bps):
        win = self.closes[max(0, i - HIGH_WIN + 1):i + 1]
        return close * BPS <= max(win) * (BPS - need_bps)

    def _primary(self, state, i, close):
        if sum(1 for e in self.trip_bars if i - e < TRIP_WIN) >= MAX_TRIPS:
            return None
        if i > LATE_CUTOFF or not self._dipped(i, close, DIP_BPS):
            return None
        div_ok = self.div_lo <= i <= self.div_hi
        cross_ok = i <= self.cross_hi
        if not (div_ok or cross_ok):
            return None
        window = self.closes[max(0, i - LONG_WIN + 1):i + 1]
        hi = max(window)
        lo = min(window)
        lo_age = next(len(window) - 1 - j for j in range(len(window) - 1, -1, -1)
                      if window[j] == lo)
        fall_bps = (1.0 - close / hi) * BPS
        above_bps = (close / lo - 1.0) * BPS
        rsi_now = self.rsi[i]
        in_cap = (fall_bps >= CAP_FALL_BPS and above_bps <= CAP_BAND_BPS
                  and rsi_now is not None and rsi_now <= CAP_RSI_MAX)
        in_ret = lo_age >= RETEST_AGE and above_bps <= RETEST_BAND_BPS
        # Day-low retest: the lowest close of the whole tape is old and
        # the price is back down near it — a double bottom, not a fresh
        # breakdown (a fresh low is young and fails the age test).
        day_lo = min(self.closes)
        day_age = next(i - j for j in range(len(self.closes) - 1, -1, -1)
                       if self.closes[j] == day_lo)
        in_day = (day_age >= DAY_AGE
                  and (close / day_lo - 1.0) * BPS <= DAY_BAND_BPS
                  and (div_ok or cross_ok))
        if not (in_cap or in_day or (in_ret and div_ok)):
            return None
        zone = "day" if in_day and not in_cap else ("cap" if in_cap else "ret")
        return self._buy_order(state, SIZE_PCT, zone, fb=False)

    def _fallback(self, state, i, close):
        # Insurance: one small trade late in the window, only if nothing
        # has filled all day — a quiet window still produces a trade.
        if self.n_buys > 0 or not (FB_FROM <= i <= FB_CUTOFF):
            return None
        if not self._dipped(i, close, FB_DIP_BPS):
            return None
        rsi_now = self.rsi[i]
        if rsi_now is None or rsi_now > FB_RSI_MAX:
            return None
        return self._buy_order(state, FB_SIZE_PCT, None, fb=True)

    # -- the per-batch decision ------------------------------------------
    def on_batch(self, state, batch):
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self._feed(int(bar["c"]))
        self._absorb_fills(batch["fills"])
        if state.warming_up or not self.closes:
            return None
        pos = state.positions.get(SYMBOL)
        if pos is not None and self.awaiting is None:
            return self._exit_order(pos)
        if pos is not None or self.awaiting is not None:
            return None
        i = len(self.closes) - 1
        if i < MIN_BARS or i < self.cool_to:
            return None
        return self._primary(state, i, self.closes[i]) or self._fallback(state, i, self.closes[i])


def main():
    holder = {}

    def on_batch(state, batch):
        bot = holder.get("bot")
        if bot is None:
            bot = holder["bot"] = DipBuyer(state.hello)
        return bot.on_batch(state, batch)

    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
