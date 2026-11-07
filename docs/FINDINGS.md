# Findings: three strategy tests, three rejections

*November 2026. All tests used rules and pass criteria declared before results were seen. Paper only; no real money.*

## TL;DR
| Test | What | Verdict | Why |
|---|---|---|---|
| **C1** | Hourly breakout on BTC/ETH (the course's rule) | ❌ Reject | Small pre-cost edge (+0.12%/trade); Alpaca crypto costs ≈ 0.54%/trade |
| **C2** | Same rule on 4-hour and daily bars | ❌ Reject | 4h still negative in both periods; daily untradeable at $100 and negative in validation at $1,000 |
| **S1** | Same rule on 14 commission-free ETFs, daily, 2016–2024 | ❌ Reject | Costs are no longer the problem; breakout entries did **worse** than entering on ordinary days |

Holding SPY beat every active variant tested over the same periods. That is a property of these tests and this rule,
not a general claim and not investment advice.

## Method
- **Same code as live.** The backtests import the live `indicators.breakout_signal` and `sizing.size_position`, so the
  test can't quietly differ from the bot.
- **No lookahead.** Signals use completed bars only. Entry is at the **next** bar's open, capped like the live IOC limit.
  A unit test asserts that truncating future data never changes past signals.
- **Conservative fills.** Stop is checked before target within a bar. Gaps through a level fill at the open. Costs are
  measured on the paper account and rounded against the strategy. A missing cost setting raises an error instead of
  defaulting.
- **Chronological splits.** Development / validation / held-out final. The final period was never evaluated because
  no rule was frozen.
- **Declared grid.** 9 stop/target combinations around the baseline, listed in advance. No "best of grid" claims.
- **Every run kept.** `data/backtest_runs.jsonl` (generated locally, not committed).

## Costs, measured not assumed
Paper smoke test, 2026-11-07, BTC/USD via the real service path:

| Item | Value |
|---|---|
| Entry | 0.000164929 BTC filled at $75,738.20 (ask $75,714.20; IOC cap $75,789.92), notional $12.49 |
| Fee charged in BTC | Held 0.000164516 after the fill: exactly 0.25% less. Sizing and the sell use the **held** quantity |
| Exit | Filled at $75,708.60 (bid $75,688.34) |
| Round trip | **$0.067 = 0.54%** of notional. The sizing model estimated 0.63%, so it was slightly conservative |
| Duplicate check | Second submit returned the same broker order id; no second order |

## C1: hourly breakout, crypto ($100 allocation)
Rule `breakout-v1`: close above the prior 20-bar high, close > EMA200, EMA50 > EMA200. Stop 2×ATR(14), target 4×ATR,
time exit 24 bars. Size from a $0.50 loss budget, capped at $25.

| Period | Trades | After costs | Before costs | Win % | Breakeven win % | Max DD |
|---|---|---|---|---|---|---|
| Development 2022-01 → 2024-11 | 422 | **−$43.77** | +$12.71 | 35.8 | 48.3 | −$46.86 |
| Validation 2024-11 → 2025-11 | 147 | **−$17.36** | +$2.53 | 32.0 | 47.3 | −$17.71 |

All 18 grid results (9 combinations × 2 periods) were negative. Costs per trade (~$0.134) were about 4.5× the pre-cost edge (~$0.03).

## C2: same rule, 4-hour and daily signals (fills and exits still simulated hourly)
| Timeframe | Development | Validation | Note |
|---|---|---|---|
| 4h, $100 | −3.6% (87 trades) | −1.2% (43) | Pre-cost +3.6% / +2.7%. Same shape as 1h, smaller |
| 1d, $100 | 0 trades | 1 trade | All 109 signals sized below Alpaca's $10 minimum (a daily stop is ~6% away, so a $0.50 risk budget buys only ~$7.60) |
| 1d, $1,000 (diagnostic) | +2.1% (21) | −1.5% (11) | Too few trades. Validation was negative even before costs |

## S1: same rule, 14 ETFs, daily, commission-free
Universe: SPY QQQ IWM DIA XLK XLF XLE XLV XLY XLP XLI XLU GLD TLT. Costs: 0 commission, 2–3 bps spread, 3 bps slippage per side.

**The pass rule was amended before any real data was fetched.** A synthetic random walk with upward drift and *no*
breakout edge passed the original rule, because any long-only rule profits in a rising market. Added requirement:
beat a "just be long" baseline (same exits/sizing/costs, entry every 5th trading day). In validation the margin must
also have Welch t ≥ 2.

| Period | Trades | Breakout, net %/trade | Before costs | Baseline, net %/trade | Breakout − baseline | $100 account | SPY buy & hold $100 |
|---|---|---|---|---|---|---|---|
| Development 2016–2021 | 951 | +0.465 | +0.558 | **+0.901** | worse, t = −2.98 | +$8.36 | +$161.81 |
| Validation 2022–2024 | 403 | +0.128 | +0.203 | **+0.242** | worse, t = −0.54 | +$6.23 | +$28.55 |

Under the original rule S1 would have "passed" both periods. The amendment prevented a false positive.

## The agent itself (shadow mode)
A real headless Claude Code run (Sonnet, Claude Code 2.1.319) against the fake broker, using the production command:
- 10 turns: loaded the skill, claimed the candidate, inspected the snapshot, recorded ENTER with numbers and evidence,
  hit the shadow-mode refusal, stopped. No permission denials, no order intents.
- **Caught a data-quality gap**: the simulated quote was 18% away from the bar close. Claude flagged it in its reason;
  the code had no rule for it. Fixed in code: quote mid > ±2% from the close now blocks entry (`quote_mismatch`).
- Cost: $0.11–0.22 per review on API billing.

## Lessons
1. **Edge-to-cost ratio decides everything at small scale.** A rule can be right more often than chance and still lose
   every month to fees.
2. **A long-only test in a rising market needs a "just be long" baseline.** Without one, S1 would have shipped.
3. **Venue minimums are strategy constraints.** The $10 crypto minimum silently made the daily variant untradeable at $100.
4. **Put safety in code, not prompts.** The AI picks ENTER/SKIP; the service owns size, price, limits, idempotency and
   reconciliation. Refusals return verbatim so the agent records SKIP instead of arguing.
5. **AI value can't be backtested honestly** on events the model has already seen. It needs prospective shadow mode.

## What would be tested next (not built)
An **earnings-drift** strategy on commission-free stocks:
- **Mechanical part** (testable now): drift after large earnings-day gaps. Dates come from SEC 8-K Item 2.02 filings.
  It would get the same baseline and declared-rule treatment as S1.
- **AI part**: Claude classifies the earnings release. It is measurable only in prospective shadow mode, over months.
- **Known limitation**: survivorship bias in any free universe of today's large-cap stocks.
