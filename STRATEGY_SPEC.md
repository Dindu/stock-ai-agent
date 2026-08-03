# Strategy Spec

This bot is a technical options system with two explicit entry playbooks. It scans symbols, scores bull versus bear structure, classifies eligible setups as breakouts or pullback continuations, ranks the valid candidates, picks a liquid option contract, opens the highest-ranked trade, then manages exits until the position is closed.

## 1. Core Idea

The bot does **not** trade on fundamentals alone. It trades when price action, momentum, regime, and option liquidity line up well enough to justify an entry.

The main signal engine is built around:
- VWAP position and VWAP direction
- EMA20 and EMA50 trend
- RSI
- volume and volume acceleration
- recent high/low breakout or breakdown structure
- momentum quality and regime alignment
- option contract liquidity and delta quality

## 2. Entry Flow

### High-level flow

```python
def run_symbol(client, symbol, prefetched_bars=None):
    bars = fetch_bars(client, symbol)
    side, data = analyze(bars, client, symbol)
    if side == "NO TRADE":
        return

    if not opening_window_exception_ok(symbol, side, data):
        return

    ok, playbook, reason = playbook_entry_ok(side, data)
    if not ok:
        return

    option = get_option_contract(symbol, side, data["price"], data, max_ext_from_vwap)
    if not option:
        return

    ok, reason = entry_contract_quality_ok(symbol, side, data, option, max_ext_from_vwap)
    if not ok:
        return

    return candidate(symbol, side, playbook, option, data)

ranked = rank_candidates(all_candidates)
try_open_paper_trade(*ranked[0])
```

### Signal generation

```python
def analyze(df, client, symbol):
    # Build indicators
    # Score bull and bear sides
    # Return the dominant side or NO TRADE
```

The scoring model uses:
- price vs VWAP
- price vs EMA20
- price vs EMA50
- EMA20 slope
- RSI
- recent high/low break structure
- volume and candle direction
- relative regime and momentum quality

### Entry playbooks

#### BREAKOUT

A new directional high or low that is aligned with VWAP, EMA20, EMA50, and the current candle. It may enter only when the directional score and dominance meet the configured floors, VWAP extension is within the configured limit, and the score is not fading.

#### PULLBACK_CONTINUATION

An established directional trend above VWAP/EMA20/EMA50 for CALLs or below them for PUTs. It may enter when the directional score is not fading and all score, dominance, extension, option-liquidity, and session safeguards pass. It does not require a new ignition event.

### Gate order

1. A valid directional CALL or PUT side must be produced by the score engine.
2. Opening and closing session rules must allow an entry.
3. The setup must qualify as `BREAKOUT` or `PULLBACK_CONTINUATION`.
4. The playbook score, dominance, directional structure, extension, and score-fade rules must pass.
5. A valid option contract must pass DTE, open-interest, delta, premium, and buying-power checks.
6. Valid candidates from the scan are ranked; only the top `MAX_NEW_ENTRIES_PER_CYCLE` candidates execute.

In two-playbook mode, legacy watchlist promotion, fresh-ignition, RSI, single-candle, ML, and repeated timing-label vetoes do not independently block an entry. They remain available only when `TWO_PLAYBOOK_ENTRY_MODE=0` restores the legacy chain.

## 3. Strategy Shape

The strategy is mostly:
- breakout/breakdown aware
- pullback-continuation aware
- directionally trend-following
- candidate-ranked before capital is committed

It tries to avoid:
- weak or ambiguous directional structure
- excessive distance from VWAP
- sharply fading trend scores
- low-liquidity or unsuitable option contracts

For ETFs and stocks, the bot has been simplified so strong directional moves are not blocked by too many redundant checks.

## 4. Code-Level Gate Functions

### Scoring and timing

```python
def analyze(df, client, symbol):
    # Computes bull/bear scores and timing context
    # Returns (side, data)
```

```python
def _classify_entry_timing(data, side):
    # Classifies the setup as:
    # EARLY_BREAKOUT, FIRST_PULLBACK, VWAP_RECLAIM,
    # BREAKDOWN, BREAKDOWN_RETEST, VWAP_REJECT,
    # MOMENTUM_CONTINUATION, LATE_CHASE, MEAN_REVERSION_RISK
```

### Playbook and candidate selection

```python
def classify_entry_playbook(side, data):
    # Returns BREAKOUT, PULLBACK_CONTINUATION, or None.
```

```python
def playbook_entry_ok(side, data):
    # Validates score, dominance, VWAP/EMA structure, extension, and score fade.
```

```python
def execute_ranked_candidates(candidates):
    # Ranks all valid candidates from the same scan before opening a position.
```

Runtime controls:

```text
TWO_PLAYBOOK_ENTRY_MODE=1
BREAKOUT_MIN_SCORE=65
PULLBACK_MIN_SCORE=65
PLAYBOOK_MIN_DOMINANCE=20
PLAYBOOK_MAX_VWAP_EXTENSION=0.012
BREAKOUT_PUT_ENTRIES_ENABLED=0
MAX_NEW_ENTRIES_PER_CYCLE=1
```

### Legacy hard score and watchlist logic

```python
def dynamic_min_required_score(symbol, side, data):
    # Returns the minimum score needed to proceed
```

```python
def watchlist_execution_confirmed(symbol, side, data):
    # Promotes WATCH setups only if the move is strong enough
```

### Contract and quality checks

```python
def get_option_contract(symbol, signal, underlying_price, data=None, max_ext_from_vwap=None):
    # Picks the best option candidate
```

```python
def entry_contract_quality_ok(symbol, side, data, option, max_ext_from_vwap):
    # Final contract-quality checklist
```

## 5. Entry State Machine

```mermaid
flowchart TD
    A[Fetch bars] --> B[Analyze symbol]
    B --> C{NO TRADE?}
    C -->|Yes| Z[Skip]
    C -->|No| D[Session safety check]
    D -->|Blocked| Z
    D -->|Passed| E[Classify entry playbook]
    E -->|No playbook| Z
    E -->|BREAKOUT or PULLBACK_CONTINUATION| F[Playbook technical gate]
    F -->|Blocked| Z
    F -->|Passed| G[Select option contract]
    G -->|No contract| Z
    G -->|Has contract| H[Contract and buying-power checks]
    H -->|Blocked| Z
    H -->|Passed| I[Rank scan candidates]
    I --> J[Execute top candidate]
```

## 6. Exit Flow

Once a trade is open, the exit monitor runs every cycle.

```python
def track_open_trades():
    for trade in open_trades:
        current_price = get_current_option_price(trade)
        pnl_pct = compute_pnl(trade, current_price)

        if pnl_pct >= target_pct:
            close_trade(trade, current_price, "TARGET HIT", pnl_pct)
        elif pnl_pct <= -stop_pct:
            close_trade(trade, current_price, "STOP LOSS", pnl_pct)
        elif partial_take_profit_reached:
            close_trade(trade, current_price, "PARTIAL TAKE PROFIT", pnl_pct, final_close=False)
        elif momentum_failed(trade):
            close_trade(trade, current_price, "MOMENTUM FAILURE", pnl_pct)
        elif trailing_stop_hit:
            close_trade(trade, current_price, "TRAILING STOP", pnl_pct)
```

### Exit rules

- Profit target closes the full trade.
- Stop loss closes the full trade.
- Partial profit can take off part of the position.
- Momentum failure closes trades when the underlying no longer supports the side.
- Trailing stop protects gains after partial profit.

### Final close bookkeeping

```python
def close_trade(trade, exit_price, reason, pnl_pct, close_qty=None, final_close=True):
    # Submit exit
    # Record Sheets / Discord
    # Preserve partial PnL if the trade had earlier partial exits
```

Final closes now write the **combined** PnL when partial exits occurred, instead of only the last leg.

## 7. Trade Lifecycle

```mermaid
flowchart LR
    A[Scan symbol] --> B[Score setup]
    B --> C[Pass entry gates]
    C --> D[Select option]
    D --> E[Open paper trade]
    E --> F[Track live PnL]
    F --> G{Target / Stop / Partial / Momentum fail?}
    G -->|Yes| H[Close trade]
    G -->|No| F
    H --> I[Write Sheets + Discord + performance log]
```

## 8. Practical Takeaway

The bot is best understood as:
- a technical scanner,
- a confirmation engine,
- an option contract selector,
- and a rules-based exit manager.

If it feels too strict, the usual reason is that several gates are stacking together. The simplified ETF and stock entry paths were added to reduce that overblocking.

## 9. Entry Pattern Matrix

These are the main chart patterns the bot should prioritize across symbols.

### 1. LOWER_HIGH_FAILURE

Use for short entries.

Required chart condition:
- strong move down already started
- bounce fails below VWAP or EMA20
- current candle breaks below prior bar low
- local micro-high stays capped under resistance

Required score condition:
- side score at least WATCH-quality
- dominance positive for PUT side
- regime not strongly against the short

Preferred timing label:
- `LOWER_HIGH_FAILURE`

Skip when:
- price is already flushing into fresh lows
- extension from VWAP is already too large
- bounce actually reclaims VWAP/EMA20

### 2. HIGHER_LOW_RECLAIM

Use for long entries.

Required chart condition:
- pullback holds trend structure
- price reclaims VWAP or EMA20
- current candle breaks above prior bar high
- local micro-low holds near support

Required score condition:
- side score at least WATCH-quality
- dominance positive for CALL side
- regime not strongly against the long

Preferred timing label:
- `HIGHER_LOW_RECLAIM`

Skip when:
- price is already vertical and extended
- reclaim is weak and immediately fading
- move is happening straight into resistance after a long run

### 3. BREAKDOWN_RETEST

Use for continuation shorts.

Required chart condition:
- prior breakdown already happened
- weak base or bounce forms
- support fails again
- price stays below VWAP and EMA20

Required score condition:
- score at least WATCH to SIGNAL quality
- delta is not fading
- bearish regime still intact

Preferred timing label:
- `BREAKDOWN_RETEST`

Skip when:
- retest is already too extended
- bearish momentum is fading hard
- support break happens after a capitulation flush

### 4. FIRST_PULLBACK

Use for clean continuation after trend confirmation.

Required chart condition:
- first real pullback after breakout or breakdown
- pullback stays controlled
- reclaim/continuation candle confirms direction

Required score condition:
- score at least WATCH to SIGNAL quality
- momentum and slope still aligned
- regime still supportive

Preferred timing label:
- `FIRST_PULLBACK`

Skip when:
- trend is already mature
- price has already traveled too far from VWAP
- overlap/chop dominates the structure

### 5. EARLY_BREAKOUT / BREAKDOWN

Use for strongest momentum expansions.

Required chart condition:
- fresh high or fresh low break
- strong directional candle
- price moving away from VWAP, not back into it

Required score condition:
- score already near strong territory
- delta expansion is real
- volume confirms

Preferred timing label:
- `EARLY_BREAKOUT`
- `BREAKDOWN`

Skip when:
- breakout is already several candles old
- the move is extended and chasey
- breakout candle immediately stalls

### Default Skip Conditions

The bot should usually skip when the chart looks like:
- chop / overlap / range noise
- blowoff top or flush bottom
- no clear reclaim or failure structure
- weak bounce in the middle of nowhere
- late score expansion after the real move already happened

## 10. Key Functions To Read In Code

### analyze

```python
def analyze(df, client, symbol):
    if len(df) < 55:
        return "NO TRADE", None

    pdh, pdl = get_previous_day_levels(client, symbol)
    df = calculate_indicators(df)

    # Scores bull and bear sides, then selects the dominant side.
```

### dynamic_min_required_score

```python
def dynamic_min_required_score(symbol, side, data):
    base = SCORE_STRONG + 1 if symbol in ETF_SYMBOLS else SCORE_STRONG + max(0, STOCK_STRONG_SCORE_BONUS)
    m = _side_metric_bundle(data, side)
    entry_timing = _classify_entry_timing(data or {}, side)

    relief = 0
    penalty = 0
    if entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "BREAKDOWN_RETEST", "VWAP_REJECT", "MOMENTUM_CONTINUATION"}:
        relief += 4

    adjusted = int(base - min(DYNAMIC_HARD_GATE_MAX_RELIEF, relief) + min(DYNAMIC_HARD_GATE_MAX_PENALTY, penalty))
    return max(int(floor), adjusted)
```

### watchlist_execution_confirmed

```python
def watchlist_execution_confirmed(symbol, side, data):
    m = _side_metric_bundle(data, side)
    entry_timing = _classify_entry_timing(data or {}, side)

    if symbol in ETF_SYMBOLS and m["delta_5m"] is not None:
        if m["side_score"] >= max(50, SCORE_WATCH - 2):
            return True, "ETF override"

    if STOCK_SIMPLE_ENTRY_MODE and symbol not in ETF_SYMBOLS and m["delta_5m"] is not None:
        if m["side_score"] >= max(48, SCORE_WATCH - 4):
            return True, "stock simplified mode"

    if entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "BREAKDOWN_RETEST", "VWAP_REJECT", "MOMENTUM_CONTINUATION"}:
        return True, f"pullback/reclaim override ({entry_timing.lower()})"
```

### get_option_contract

```python
def get_option_contract(symbol, signal, underlying_price, data=None, max_ext_from_vwap=None):
    # Fetches candidate contracts from Alpaca, ranks them, and rejects weak liquidity.
    # If the strict path fails, it can fall back to alert-only mode.
```

### entry_contract_quality_ok

```python
def entry_contract_quality_ok(symbol, side, data, option, max_ext_from_vwap):
    if not option:
        return False, "missing option contract"

    if STRICT_IGNITION_NO_BYPASS and IGNITION_REQUIRED and not bool((data or {}).get("ignition_confirmed", False)):
        return False, "ignition not freshly confirmed"

    if ENTRY_ALLOWED_SETUPS and entry_timing not in ENTRY_ALLOWED_SETUPS:
        return False, f"setup {entry_timing} not in allowed setups"
```

### try_open_paper_trade

```python
def try_open_paper_trade(symbol, side, option, data):
    if not ENABLE_ALPACA_PAPER_TRADING:
        return False

    sync_open_trades_from_alpaca()
    qty = position_qty_from_score(score, dominance)
    _, fill_price = place_paper_entry(option, qty)
    trade = open_trade_record(symbol, signal_label, option, score, fill_price, qty, data=data)
```

### track_open_trades

```python
def track_open_trades():
    for trade in list(_open_trades.values()):
        current_price = get_current_option_price(trade)
        pnl_pct = (current_price - trade["entry"]) / trade["entry"]

        if pnl_pct >= target_pct:
            close_trade(trade, current_price, "TARGET HIT", pnl_pct)
        elif pnl_pct <= -stop_pct:
            close_trade(trade, current_price, "STOP LOSS", pnl_pct)
        elif partial_condition_met:
            close_trade(trade, current_price, "PARTIAL TAKE PROFIT", pnl_pct, final_close=False)
```

### close_trade

```python
def close_trade(trade, exit_price, reason, pnl_pct, close_qty=None, final_close=True):
    close_qty_int = int(max(1, min(current_qty, int(close_qty or current_qty))))
    if final_close and _prev_partial_qty > 0 and _combined_cost > 0:
        combined_pnl_pct = _combined_dollar / _combined_cost

    update_alert_close_to_sheets(combined_row, trade)
    log_trade_to_sheets(combined_row, trade, final_close=True)
```

### run_symbol and run_websocket_cycle

```python
def run_symbol(client, symbol, prefetched_bars=None):
    bars = prefetched_bars if prefetched_bars is not None else fetch_bars(client, symbol)
    side, data = analyze(bars, client, symbol)
    # Then applies watchlist, hard-score, ignition, RSI, anti-chase, candle, and contract gates.
```

```python
def run_websocket_cycle(client):
    # Enqueue symbols from ticks, periodically full-scan, and call track_open_trades().
    while True:
        symbols_to_run = _order_symbols_by_priority(symbols_to_run)
        prefetched_bars = prefetch_bars_parallel(client, symbols_to_run)
        for symbol in symbols_to_run:
            run_symbol(client, symbol, prefetched_bars=prefetched_bars.get(symbol))
```
