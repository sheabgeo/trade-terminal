# Paper Trading Rulebook — v1 (fixed through first evaluation)

| Setting | First paper version | Where it's enforced |
|---|---|---|
| Markets | BTC/USD, ETH/USD | `strategy.yaml` → collector only evaluates these |
| Signal interval | Completed hourly candles | Collector drops any bar whose hour hasn't closed |
| Direction | Long only (spot, no margin, no shorts) | Order service only builds BUY entries / SELL exits of held qty |
| Open positions | One at a time | Service refuses ENTER if any strategy position or open order exists |
| Strategy allocation | $100 | `risk.yaml`; sizing uses this, never full account equity |
| Max position value | 25% of allocation ($25) | Hard cap in sizing (stage 4) |
| Loss budget per trade | 0.5% of allocation ($0.50) incl. est. costs | Sizing formula (stage 4) |
| Daily loss halt | 2% of start-of-day UTC strategy equity, incl. unrealized + fees | Supervisor (stage 5) |
| Live trading | Disabled | Adapter is hard-wired to paper; config refuses `live_trading: true` |

## How each setting affects an order
- **Markets / interval:** no other symbol or timeframe can produce a candidate.
- **Long only:** the only entry is a buy with available cash; exits only sell what the broker says is held.
- **One position:** a second candidate while a position or unresolved order exists is blocked, not queued.
- **Allocation $100:** a $100k paper account still trades as if it had $100.
- **25% cap:** even if the loss budget allows more, position value ≤ $25.
- **0.5% loss budget:** qty = $0.50 ÷ (stop distance + est. fees/slippage per unit), rounded **down** to the venue increment; below venue minimum → SKIP.
- **2% daily halt:** blocks new entries for the rest of the UTC day; a price bounce does not reset it.

## Strategy (hourly breakout in uptrend), version `breakout-v1`
Candidate when the **completed** bar:
1. closes above the max high of the **previous 20** completed bars (signal bar excluded), and
2. closes above EMA(200), and
3. EMA(50) > EMA(200).

Exits (stage 4–5): stop = entry − 2×ATR(14); target = entry + 4×ATR(14); time exit after 24 bars. Bot may request an earlier exit with a reason; it may never widen the stop.

## Data blockers (any one blocks ENTER)
- Quote older than 15 s at snapshot time
- Missing hourly bar in the indicator window
- Unresolved open order, or existing position
- Candidate past its expiry

## Provider facts (Alpaca, verified on paper 2026-11-07)
- Crypto supports market/limit/stop-limit with `gtc`/`ioc` time-in-force; **no bracket orders for crypto** — exits are managed by our supervisor.
- Fees can be charged in the acquired asset → sellable qty may be slightly less than bought qty.
- **Minimum order value $10** (Alpaca error 40310000, confirmed on paper 2026-11-07) — sizing below $10 → SKIP.
- Paper access does not imply live crypto eligibility in Texas/your account.
