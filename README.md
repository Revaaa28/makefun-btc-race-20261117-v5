# Capitulation + retest dip-buyer — BTC race bot (v5 design)

Entry for the make.fun competition **btc-race-24h-20261117-0603**
("Race a BTC trading bot live for 24 hours"): a Python bot for the
`agent-fun/bars-replay/v1` protocol that trades Coinbase Wrapped BTC
(cbBTC) on 1-minute bars with $1,000 of paper USDC — long only, at most
one position, no network, no wall clock, fully deterministic.

This is the **v5 design** first built for the 2026-11-13 race
(capitulation + divergence retest dip-buyer, plus the day-low retest
zone) — same zones, same parameters, same native exits — freshly
reimplemented for this edition and re-validated on this edition's dev
window before submitting. The reimplementation's behaviour was
verified order-for-order identical to the original bot's (every fill,
on every window tested) before it was trusted.

## Strategy

A buy is considered only when the latest close sits at least 50 bps
below the trailing 45-bar high **and** momentum says the fall is
exhausting (a bullish RSI(14) divergence at confirmed swing lows, or an
RSI cross-up through 30), **and** the price is inside one of three
zones measured on the 240-bar long window:

- **Capitulation** — the close has fallen at least 340 bps from the
  long high, sits within 40 bps of the long low, and RSI ≤ 28: a
  washed-out bottom, bought as it prints.
- **Retest** — the long low is at least 60 bars old and the close is
  back within 170 bps above it; divergence entries only (a falling
  knife through the level prints no divergence and is skipped).
- **Day-low retest** — the lowest close of the whole window is at
  least 240 bars old and the close is back within 50 bps above it: a
  double bottom against the day's low, confirmed by either momentum
  signal. These exit faster (TP +90 bps) because the bounce is shorter.

Exits are measured against the position's cost basis (fees included):
take profit **+150 bps** / stop loss **−100 bps** for capitulation and
retest entries, **+90 / −100 bps** for day-low entries. Entries are
all-in (100% of cash), at most 3 per 1,440 bars, with a 5-bar cooldown
after every exit and no entries after bar 1,250. An insurance fallback
takes one small (20%) trade late in the window (bars 1,150–1,400,
TP +80 / SL −50) only if nothing has filled all day, so a quiet window
still produces a trade.

## Validation on this edition's dev window

Graded locally with a fill-faithful simulator of the kit harness
(next-bar-open fills, 4 bps fee + 2 bps slippage + constant-product
pool impact, end-of-window flatten):

- **Dev window: net PnL +$13.48** — 1 round trip (retest zone,
  bars 578 → 959), fees $0.81, max drawdown 0.62%. Two stdio
  end-to-end runs byte-identical. A fill-by-fill comparison against
  the original 2026-11-13 bot on this window and on all eight days
  below matched exactly (same orders, same fills, same PnL to the
  micro-USDC).
- The same bot across 8 real Binance BTCUSDT 1-minute days
  (2026-11-09 → 2026-11-16, constant pool depth = the dev window's
  median) totals **+$0.12** — near-flat, worst day −$14.72, worst
  drawdown 1.90%. It is a selective survivor, not a hot hand: it
  trades rarely and keeps drawdowns near 1%.

## Layout

- `main.py` — the bot (standard library only).
- `vendor/makefun_bars.py` — the kit SDK, vendored unchanged
  (sha256 80452b00d2eb1e9c959e73d41cda6ca726cdbe7bfa4065d48278c22eccd276ff).

## License

MIT — see `LICENSE`.
