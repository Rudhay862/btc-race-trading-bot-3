"""Donchian breakout bot for btc-race-24h-20261115-0828.

Trades CBBTC/USDC on 1-minute bars, long only, full size, one position at a time.

Idea: BTC trends in bursts on this pool and whipsaws the rest of the time, while a
full-size round trip costs roughly 26-30 bps in fee, slippage and price impact. So
the bot only trades a confirmed breakout and exits fast when the move fails:

  * Enter: the close breaks above the highest high of the previous 60 bars
    (a 1-hour breakout) after at least 30 bars have passed since the last exit.
    No new entry in the final 30 bars of the 1,440-bar window: a trade entered
    there would be flattened almost immediately at full round-trip cost.
  * Exit: the close falls below the lowest low of the previous 15 bars, or the
    close drops 150 bps (1.5%) below the highest close seen since entry
    (trailing stop, a safety net against a sharp reversal).

Exit bookkeeping (cooldown start and peak reset) is driven by the position
actually observed in the account state, not by the sell order sent: a sell
can be rejected (e.g. max slippage), and in that case the bot is still
holding, so the peak must be kept and the cooldown must not start early.
Only the holding -> flat transition counts as an exit, dated at the bar
where flatness is first observed.

Fee accounting: a full-size round trip costs roughly 26-30 bps (4 bps fee
each way, 2 bps slippage each way, plus constant-product price impact on a
~$1,000 order against the pool depth). The breakout must therefore clear
more than that to pay; the 60-bar breakout plus fast 15-bar breakdown exit
is the filter that keeps the bot out of chop.

Window slicing note: every lookback slice is [...-N-1:-1], i.e. the N bars
BEFORE the current bar, never including the current bar's own high/low —
including it would let a bar break out above its own high, which is not a
breakout at all.

Everything is integer math on the bar prices the harness sends, so the bot is
fully deterministic: the same bars always produce the same orders. No clock, no
network, no randomness, no files outside the repository.

Parameters (chosen from a small robustness check on the dev window, not a fine
search: the neighbouring breakout lengths 60-75 bars and exit lengths 10-20 bars
are all profitable on the dev window):
  HI_BARS   = 60   breakout lookback (bars)
  LO_BARS   = 15   breakdown lookback (bars)
  TRAIL_BPS = 150  trailing stop from the peak close since entry (bps)
  COOLDOWN  = 30   bars to wait after an exit before re-entering
  MAX_SLIP  = 1000 max slippage allowed on any order (bps)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

HI_BARS = 60
LO_BARS = 15
TRAIL_BPS = 150
COOLDOWN = 30
MAX_SLIP = 1000
WINDOW_BARS = 1440  # documented length of the dev and race windows
NO_ENTRY_LAST = 30  # bars at the end of the window with no new entries


class DonchianBot:
    def __init__(self):
        self.highs = []          # high of each bar seen, in price units
        self.lows = []           # low of each bar seen, in price units
        self.closes = []         # close of each bar seen, in price units
        self.peak_close = 0      # highest close since entry (0 when flat)
        self.last_exit_seq = -10 ** 9  # seq of the last observed exit
        self.holding = False     # whether a position was observed last bar

    def on_batch(self, state, batch):
        bar = None
        for b in batch.get("bars", []):
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        # A bar with a missing/None h, l or c carries no usable price: skip
        # it entirely instead of crashing on int(None) or appending a bogus
        # value that would corrupt the Donchian windows.
        if bar.get("h") is None or bar.get("l") is None or bar.get("c") is None:
            return []
        self.highs.append(int(bar["h"]))
        self.lows.append(int(bar["l"]))
        self.closes.append(int(bar["c"]))
        close = self.closes[-1]

        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding is not None else 0

        # Exit bookkeeping is driven by the position we OBSERVE, not by the
        # sell order we sent: a sell can be rejected (e.g. max slippage), in
        # which case we are still holding and neither the peak nor the
        # cooldown may move. Only the holding -> flat transition counts as
        # an exit, dated at the bar where flatness is first observed.
        if qty == 0 and self.holding:
            self.last_exit_seq = state.seq
            self.peak_close = 0
        self.holding = qty > 0

        if state.warming_up:
            return []

        if qty > 0:
            # Track the peak close while holding, for the trailing stop.
            if close > self.peak_close:
                self.peak_close = close
            # Exit: breakdown below the recent lows, or trailing stop.
            need = len(self.lows)
            if need > LO_BARS:
                recent_low = min(self.lows[-LO_BARS - 1:-1])
                trail_hit = (self.peak_close - close) * 10000 >= TRAIL_BPS * self.peak_close
                if close < recent_low or trail_hit:
                    return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                             "max_slippage_bps": MAX_SLIP}]
            return []

        # Flat: look for a breakout above the previous HI_BARS highs.
        # The slice [-HI_BARS-1:-1] is the 60 bars BEFORE this one: the
        # current bar's high must not count toward its own breakout, and
        # the close (not the high) must clear the level to confirm it.
        if (len(self.highs) > HI_BARS and state.seq - self.last_exit_seq >= COOLDOWN
                and state.seq < WINDOW_BARS - NO_ENTRY_LAST):
            breakout_high = max(self.highs[-HI_BARS - 1:-1])
            if close > breakout_high and state.cash > 0:
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": state.cash,
                         "max_slippage_bps": MAX_SLIP}]
        return []


def main():
    bot = DonchianBot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
