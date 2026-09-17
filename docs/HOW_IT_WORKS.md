# Historical strategy walkthrough

This walkthrough describes the original system. For current execution, research
and learning behavior, [Architecture](ARCHITECTURE.md) and
[Learning policy](LEARNING.md) take precedence.

# How qmag decides: the flow from the whole market to one order

This is the plain-language walk-through of what the program does every day,
how a stock gets from "one of ~5,000 listings" to "a sized order at the
broker", what can stop it at each step, and which settings move each step.
Setting names are written the way they appear on the settings page and in
`settings.yaml` (`section.key`); the value in brackets is the default.

Every number below is read from real bars, real API answers or the real
broker account. When a source did not answer, the program records a *data
gap* and treats the missing fact as "unknown", never as "fine" — a gate that
needs evidence fails closed.

---

## 0. The clock: what runs when

The daemon (`qmag daemon`) runs seven kinds of task on the New York market
calendar (weekdays that are not NYSE holidays; early closes respected).

| When (ET) | Task | What it looks at | Settings |
| --- | --- | --- | --- |
| 08:30, 09:20 | **Pre-market gap scan** | One screener request for stocks gapping ≥ 8 % on heavy early volume. Hits join the *arming list* as episodic-pivot candidates. | `schedule.premarket_enabled` [on], `premarket_times` [08:30, 09:20], `premarket_min_gap_pct` [0.08], `premarket_max_names` [25] |
| 09:40 | **Post-open full scan** | The whole universe on the day's first bars, so an earnings gap no screener surfaced is still caught. | `schedule.post_open_full_scan` [on], `post_open_time` [09:40] |
| 09:35 → close − 5 min, every 5 min | **Focused cycle** | Only the arming list, open positions, pending entries and today's screener hits. This is the pass that actually buys. | `schedule.focused_interval_minutes` [5], `focused_start` [09:35], `focused_stop_before_close_minutes` [5], `trigger_distance_pct` [0.02] |
| 10:00 → close − 30 min, every 30 min | **Movers sweep** | One screener call for the day's top gainers (≥ 8 % on ≥ 3× relative volume). New names get the full check and join the arming list. | `schedule.movers_enabled` [on], `movers_interval_minutes` [30], `movers_min_change_pct` [0.08], `movers_min_rvol` [3.0], `movers_max_names` [25] |
| Close + 20 min | **Nightly arming scan** | The whole universe on completed bars: detect, rank, size and chart tomorrow's plans, rebuild the arming list. | `schedule.arming_distance_pct` [0.05], `arming_max_names` [60] |
| Saturday 10:00 | **Insider scan** | The week's most unusual options trades (see §9). | `insider_scan.*` |
| Saturday 11:00 | **Learning review** | Journal + shadow trades → lessons and bounded knob adjustments (see §8). | `learning.review_weekday` [saturday], `review_time` [11:00] |
| Sunday 12:00 | **Universe rebuild** | Refresh the whole-market ticker list. | (always on) |

`schedule.tiered: false` switches to the old plan (09:45 post-open, whole-
universe pass every 30 minutes, one after-close cycle). Every time here is
read from the live config, so a change on the settings page applies at the
next tick.

---

## 1. The universe: what is even looked at

* `qmag universe build` pulls every NASDAQ / NYSE / NYSE American / Arca /
  BATS listing from Nasdaq Trader's public directories, drops ETFs,
  warrants, rights, units, preferreds, notes and funds, downloads a year of
  bars for the rest and keeps the tradeable ones: **last close ≥ $3** and
  **20-day average dollar volume ≥ $5 M**. The result is
  `universe/market.txt` — currently 2,860 of the 5,600 listings (built 2026-09-11).
* With `--fundamentals` it also caches finviz sector / industry / float /
  short interest, which gives the theme engine one theme per industry.
* Daily bars come from the data source in `QMAG_DATA` (`auto` = Interactive
  Brokers when the gateway answers, else Unusual Whales with a key, else
  Yahoo). Bars that are **stale** (the source did not deliver today's bar)
  block new entries for that pass: exits are still managed, but every new
  plan fails `price_data_fresh`.

Settings: the two universe floors are the same as `momentum.min_price`
[3.0] and `momentum.min_dollar_volume` [5,000,000].

---

## 2. Indicators: what is computed for every stock

For each symbol's daily bars the program computes: `gain_1m/3m/6m`
(21 / 63 / 126-bar % change), `adr_pct` (20-day average daily range, high/low
− 1, in %), `adr_dollar`, `rvol_20` and `rvol_50` (today's volume vs the
20- / 50-day average), `dollar_vol_20`, `gap_pct` (open vs prior close),
`sma_10` / `sma_20` (and the trailing MA), and — when themes are on — the
stock's `theme`, `theme_pct` and `theme_breadth` (§4).

Intraday, when a bar is partial, `rvol` is judged on **pace**: today's
volume so far is projected to a full day using the normal time-of-day
volume curve, so a stock doing 1.5× its average volume by 10:30 is read as
1.5×, not 0.3×. Before ~9:44 the projection is too noisy and raw volume is
used (`schedule.volume_pace` [on], `pace_min_session_fraction` [0.08]).

---

## 3. Stage one filter: is this a momentum leader?

A stock is only *eligible* for the breakout setup if, on the bar before the
signal:

* it gained **≥ 30 % in 1 month** OR **≥ 50 % in 3 months** OR **≥ 100 % in
  6 months** (`momentum.min_gain_1m` [0.30], `min_gain_3m` [0.50],
  `min_gain_6m` [1.00]);
* its **ADR ≥ 3.5 %** (`momentum.min_adr_pct` [3.5]) — Kullamägi prefers
  > 5; the floor is deliberately lower so the ranking, not the filter,
  decides;
* price ≥ $3 and 20-day dollar volume ≥ $5 M (`min_price`, `min_dollar_volume`).

This is the "top 1–2 % of the market" filter. Episodic pivots (§5b) skip it
on purpose: an EP is a *neglected* stock that just became interesting.

---

## 4. Market and theme backdrop

**Regime** (`regime.enabled` [on]) — three independent gates, all must pass
for new longs; a gate whose data is missing makes the regime *unknown*,
which is treated as risk-off:

* benchmark trend: **QQQ above its 20-day MA** (`regime.benchmark` [QQQ],
  `ma_length` [20]);
* breadth: **≥ 40 % of the scan universe above its 20-day MA**
  (`breadth_enabled` [on], `breadth_ma_length` [20], `min_breadth` [0.40]);
* volatility: VIX below a ceiling, off by default (`max_vix` [None]).

When the regime is off, the cycle still runs (exits are managed, the arming
list is refreshed, plans are built and shown as rejected on
`market_regime`) but nothing is bought.

**Themes** (`themes.enabled` [on]) — ticker groups from `universe/themes.yaml`
plus one theme per finviz industry (`use_industries` [on],
`min_industry_members` [4]). Each theme's index is the **median** member's
daily return (one stock tripling cannot make its industry look hot); themes
are ranked on a 1-month / 3-month momentum composite; a theme with fewer
than `min_theme_members` [3] scanned members does not rank. A stock inherits
the best percentile among its themes.

* Hard gate, **breakouts only** (`apply_to` [breakout]): skip if the best
  theme is in the bottom 30 % (`min_theme_percentile` [0.3]) or fewer than
  40 % of the theme's members are above their 20-day MA
  (`min_theme_breadth` [0.4]). A stock in no theme passes unless
  `require_theme` [off].
* Soft ranking: `theme_pct × score_weight` [1.0] is added to every signal's
  score, so of two equal flags the one in the hotter group gets the slot.

---

## 5. Setup detection: does it have a trigger?

Two detectors run on every eligible stock. Each returns a *Signal*: pivot,
entry price, initial stop, ADR and a score.

### 5a. Momentum breakout (`breakout.enabled` [on])

Looking back from today's bar, the detector searches for the **shortest
valid flag** ending yesterday:

1. Structure: yesterday's close above the 20-day MA and the 10-day MA rising
   over the last three bars (`require_above_ma` [on], `fast_ma` [10],
   `slow_ma` [20]).
2. Flag length **10–60 bars** (`min_flag_days` [10], `max_flag_days` [60]).
3. Flag depth (flag high − flag low) ≤ **50 % of the prior impulse** (the
   move from the previous quarter's swing low to the flag high) —
   `max_flag_depth` [0.5]. Deeper = disorderly base, skipped.
4. The flag high must be a genuine high: within 3 % of the 6-month high.
5. Range contraction: the last 5 days' ADR ≤ **85 %** of the flag's ADR
   (`max_recent_adr_ratio` [0.85]).
6. Not already broken out inside the flag.

Trigger, on today's bar: high ≥ pivot × (1 + `entry_buffer_pct` [0.002]),
open not more than **5 % above the pivot** (`max_gap_pct` [0.05] — don't
chase), and volume ≥ **1.2× the 20-day average** (`min_breakout_volume_ratio`
[1.2]; intraday the lower `entry.confirm_volume_ratio` is used at detection
so the confirmation gate, not the detector, has the final say).

Entry = max(open, pivot + buffer). Stop = entry − **1.0 × ADR$**
(`management.stop_adr_mult` [1.0]) — the low-of-day approximation.

**Watchlist** form: the same flag test on the *latest* bar with a
hypothetical tomorrow; only names whose close is within 10 % of the pivot
are listed. These are the flags that get armed overnight.

Score (used only for ranking): `gain_1m + 0.5·gain_3m + 0.25·gain_6m +
adr_pct/100 + theme bonus + sentiment bonus`.

### 5b. Episodic pivot (`episodic_pivot.enabled` [on])

On the current bar (which may be today's partial bar):

* gap ≥ **10 %** over yesterday's close (`min_gap_pct` [0.10]);
* volume ≥ **3× the 50-day average** (`min_volume_ratio` [3.0]) — intraday
  judged on pace;
* the stock was **not already extended**: prior 3-month gain ≤ 30 %
  (`max_prior_gain_3m` [0.30]);
* price ≥ $3, and dollar volume × rvol ≥ the $5 M floor;
* today's high already cleared open × (1 + `entry_buffer_pct` [0.002]).

Entry = open + buffer. Stop = entry − max(1 ADR$, 25 % of the gap), so a
30 % gapper does not get an absurdly tight stop. Score = gap × rvol (+ theme
/ sentiment bonus): the size of the surprise. EPs cannot be pre-planned from
daily bars, so they only ever come from the intraday passes and the
screeners.

Optional **sentiment** CSV gate/bonus (`sentiment.*`, off by default) applies
to both setups.

---

## 6. From signal to plan: the checklist and the sizing

Every signal — triggered now, or a watch flag armed for tomorrow — becomes a
*TradePlan* with a checklist. **Every check must be true** for the plan to
be `ok`. A plan that fails is recorded as *rejected* with the names of the
failed checks (and followed as a shadow trade, §8).

### 6a. Live context (paid / free reads for the best candidates only)

Context is gathered for the triggered + near-pivot names, best score first,
capped at `context.max_symbols_per_cycle` [40] (options flow
`options_flow.max_symbols_per_cycle` [25], edge `edge.max_symbols_per_cycle`
[20]), cached for `context.cache_minutes` [30]:

* **News** (finviz, Yahoo fallback): headline tone over
  `news_lookback_days` [5] → `news_score` −1..+1.
* **Social** (StockTwits; Reddit with keys): tone and message count.
* **Events / fundamentals**: next earnings date, float, short interest,
  sector / industry, analyst mean.
* **Options flow** (Unusual Whales): the day's bullish vs bearish premium,
  call/put volume vs 30-day average, unusual alerts (vol > OI, opening,
  OTM, ask-side, ≥ $50 k, ≤ 90 DTE) → `flow_score` −1..+1 and an
  `unusual` flag. Settings: `options_flow.min_premium` [50,000],
  `lookback_days` [3], `max_dte` [90], `sweep_weight` [1.5].
* **Edge score** (Unusual Whales, ~20 requests per name): 21 features, each
  turned into a −1..+1 sub-score and weight-averaged — flow 1.5, net
  premium 1.0, OI change 0.75, dark pool 0.75, option sentiment 0.75,
  market tide 0.75, gamma / dealer delta / skew / options pulse / relative
  volume / short interest / insiders / institutions / analysts / earnings /
  news / sector tide 0.5, max pain / volatility / congress / seasonality
  0.25 (`edge.weights`, 0 = off). **Coverage** = share of the enabled weight
  that actually answered.

The **composite context score** blends news (1.0), social (0.6), flow
(1.0) and edge (1.0) over whatever answered and is added to the plan's
ranking score × `context.score_weight` [0.5].

### 6b. The checklist

| Check | Passes when | Settings |
| --- | --- | --- |
| `setup_triggered` | a detector fired | — |
| `momentum_leader` | §3 (breakouts only) | `momentum.*` |
| `theme_strength` | §4 theme gate | `themes.*` |
| `sentiment` | CSV sentiment within bounds | `sentiment.*` |
| `market_regime` | §4 regime OK **and known** | `regime.*` |
| `liquidity` | 20-day dollar volume ≥ floor (unknown = fail) | `momentum.min_dollar_volume` |
| `price_floor` | close ≥ `momentum.min_price` | |
| `stop_within_2_adr` | stop distance ≤ 2 × ADR% | — |
| `stop_distance_ok` | 0 < stop distance ≤ **15 %** | `management.max_stop_pct` [0.15] |
| `earnings_window` | breakout not within **3 days** of earnings | `context.avoid_earnings_within_days` [3] |
| `news_flow` | news score ≥ **−0.5** (unknown passes) | `context.news_min_score` [−0.5] |
| `social_crowding` | EP not at near-unanimous bullish chatter (> 0.85) | `context.social_max_score` [0.85], `social_min_score` [None] |
| `options_flow` | flow score ≥ `options_flow.min_score` [None = record only] | |
| `unusual_flow_bullish` | only with `options_flow.require_bullish` [off]: bullish unusual flow **must be seen** (missing = fail) | `bullish_threshold` [0.2], `min_alerts` [1] |
| `uw_edge_coverage` | ≥ **50 %** of the enabled edge weight answered | `edge.min_coverage` [0.5] |
| `uw_edge` | edge score ≥ **+0.15**. Both edge checks are **armed only when an Unusual Whales key is saved**; without one the plan carries a data gap saying the gate is not applied, instead of a check that could never pass | `edge.gate` [on], `edge.threshold` [0.15] |
| `size_positive` | the sizing below yields ≥ 1 share | `risk.*` |
| `price_data_fresh` | today's bar was delivered | data source |
| `broker_account` | equity and cash could be read | broker |
| `committee` | with `committee.can_veto`: chair did not reject | §7 |
| `llm_reviewer` | with `reviewer.mode` gate/gate_and_size: BUY at ≥ min confidence | §7 |

Note the asymmetry that matters: *soft* facts (news, social, flow) pass
when unknown; the *edge gate* and `require_bullish` fail when unknown,
because a threshold cannot be met by numbers that were never sourced. The
one exception is structural: a desk with **no Unusual Whales key at all**
cannot compute the score by construction, so there the edge gate stands
down (loudly, in every plan's data gaps and in the header pill) rather than
rejecting every trade for ever.

### 6c. Position size

```
risk_pct   = risk.risk_per_trade_pct [0.005] × adaptive multiplier, capped at adaptive.max_risk_per_trade_pct [0.01]
shares     = equity × risk_pct ÷ (entry − stop)
shares     = min(shares, equity × risk.max_position_pct [0.25] ÷ entry)       # concentration cap
shares     = min(shares, remaining room under risk.max_gross_exposure [1.0], cash ÷ entry)
```

So with $100 k equity, 0.5 % risk and a stop 6 % below entry, the position is
~$8.3 k and the loss at the stop is $500.

**Adaptive risk** (`adaptive.enabled` [on]): the average R of the last 10
closed trades (`lookback_trades` [10], needs `min_trades` [6]) scales risk:
≤ −0.2 R → ×0.5 (`cold_avg_r`, `cold_risk_mult`); ≥ +0.6 R → ×1.25
(`hot_avg_r`, `hot_risk_mult`). "Size up when the market is paying you."

The plan also fixes the **partial target** (entry + 2 R,
`management.partial_target_r` [2.0]), the partial quantity (33 %,
`partial_fraction` [0.33]) and the trailing MA (10-day, `trail_ma` [10]).

### 6d. Portfolio gates (judged at the moment of entry)

* **Slots**: at most `risk.max_positions` [8] open + resting positions.
* **Heat**: the sum of what every open position and resting entry would
  lose at its stop, plus this plan's risk, ≤ **4 % of equity**
  (`risk.max_portfolio_heat_pct` [0.04]).
* **Daily loss circuit-breaker**: once equity is down **3 %** from the
  session's first pass, no new entries until tomorrow
  (`risk.daily_loss_limit_pct` [0.03]).
* **Theme concentration**: at most **3** open/pending positions in one
  theme (`risk.max_positions_per_theme` [3]).

---

## 7. The AI layer (optional, additive)

All of this runs *after* the deterministic checklist and only on plans the
rules already like. Every seat picks its provider and model on the settings
page (AI providers → Language models). No seat is allowed to invent facts:
each receives exactly the plan, checklist, rationale and gathered context.

* **Analyst committee** (`committee.enabled` [off]): three separate model
  calls per plan — a **bull researcher** argues for, a **bear researcher**
  argues against after reading the bull case (`debate` [on]), and a **risk
  chair** rules `take | reduce | reject` with a size multiplier. Advisory
  unless `can_veto` [off]; `max_plans_per_cycle` [8] caps the cost. Giving
  the seats different vendors makes the debate real.
* **Trade reviewer** (`reviewer.enabled` [off]): one model reads the whole
  edge bundle — setup, sizing, checklist, context, committee debate,
  portfolio state — and answers BUY / SELL / HOLD with confidence, thesis,
  catalysts, risks, invalidation. `mode` [advisory] records it;
  `gate` requires BUY ≥ `min_confidence` [0.6]; `gate_and_size` also scales
  shares by the model's sizeMultiplier. `fail_closed` [off] decides whether
  an unreachable model blocks the trade. `review_watchlist` [on] also
  reviews tomorrow's armed plans.

---

## 8. Entry: how a plan becomes an order

`entry.mode` decides the mechanics:

* **`confirmed`** [default] — nothing rests at the broker. Armed plans wait;
  a focused pass buys **at market** only when the live bar shows all of:
  price above the pivot **and still holding there** (`require_hold_above_pivot`
  [on] — a wick does not count), not more than `breakout.max_gap_pct` above
  the pivot, outside the first **10 minutes** (`opening_range_minutes` [10]),
  and volume pacing to ≥ **1.0×** the 20-day average
  (`confirm_volume_ratio` [1.0]). A plan that is triggered but not confirmed
  is reported as HOLD with the reason and re-checked five minutes later.
* **`resting`** — the nightly scan parks **stop-limit buy brackets** at the
  broker (stop at the pivot, limit at pivot × 1.05, protective stop
  attached). Never misses a fast move; takes more false breakouts. Only
  plans scoring ≥ `resting_min_score` [0] rest. An unchanged plan keeps its
  order (no churn); a moved pivot or stop replaces it; a name that dropped
  off the list is cancelled.
* **`hybrid`** — nothing overnight; buy-stops are parked by the first
  focused pass at or after `resting_from` [09:40] and expire at the close.

Order of preference inside a pass: triggered setups first (best score
first), then armed watch plans. Each buy consumes a slot, adds to exposure
and heat, and the next candidate is judged against the updated numbers.

**Arming list**: after every full scan the watch flags within
`schedule.arming_distance_pct` [5 %] of their pivot, anything that
triggered, and the screener hits become the arming list (top
`arming_max_names` [60] by score). The focused passes spend context / paid
edge reads only on armed names within `trigger_distance_pct` [2 %] of the
pivot; the rest are left as they are.

---

## 9. Managing the position: the exit playbook

Every open position is checked on every pass, in this order:

1. **Failed breakout** (`entry.failed_breakout_exit` [on]): a fresh position
   (held ≤ `failed_breakout_days` [1] bars, no partial yet) whose close is
   back **below the pivot by > 0.5 %** (`failed_breakout_tolerance_pct`
   [0.005]) is sold at market — the small fast loss instead of waiting for
   the full stop. Intraday this needs the volume pace to be measurable.
2. **Time stop** (`management.time_stop_days` [5]): after 5 completed
   sessions with the close at or under entry and never having shown 1 R of
   open profit (`time_stop_min_mfe_r` [1.0]), out at the close — dead money
   is risk and slot cost. 0 = off.
3. **Partial into strength**: a resting limit at **entry + 2 R**
   (`partial_target_r` [2.0]) sells 33 % (`partial_fraction` [0.33]) on a
   spike; otherwise at the close of day **3** (`partial_after_days` [3]) if
   the trade is green. Either way the stop then moves to **breakeven**
   (`move_stop_to_breakeven` [on]).
4. **Trail**: after the partial, the rest is sold when the close drops below
   the **10-day MA** (`trail_ma` [10]; 20 for slower names).
5. **Max hold**: anything still open after **60** bars is closed
   (`max_hold_days` [60]).
6. The protective **stop** (initial 1 ADR below entry, then breakeven) rests
   at the broker the whole time; a stop-out is detected at the next pass and
   booked with its reason.

Every exit records the reason, hold time, best / worst excursion in R and
the features the entry was taken on — that is the trade journal.

---

## 10. Learning: what the journal does to the settings

`learning.enabled` [on]. Three sources of evidence:

* **Closed trades** with their entry features (setup, entry mode, scan
  source, paced rvol, minutes since open, ADR, flag depth, gap, theme
  percentile, edge score, reviewer verdict, regime, …).
* **Shadow trades** (`shadow_enabled` [on]): every plan the checklist
  rejected, a gate held, or a slot refused is followed on real bars for
  `shadow_max_days` [5] / `shadow_hold_days` [10], so a filter is judged on
  what it *blocked*, not only on what it let through.
* **Near misses**: setups that would have triggered one step looser on a
  detector threshold, recorded nightly.

The Saturday review buckets everything by feature, reports each bucket's
lift over the overall expectancy (shrunk towards zero for small samples) and
writes plain-English lessons. With `auto_apply` [on] it moves **one step at a
time**, only where ≥ `min_trades` [8] trades show ≥ `min_lift_r` [0.25] R of
lift, and only within hard bounds, these eight knobs:

`breakout.min_breakout_volume_ratio` (1.0–2.5), `entry.confirm_volume_ratio`
(0.8–2.0), `entry.opening_range_minutes` (0–30), `edge.threshold`
(−0.2–0.6), `themes.min_theme_percentile` (0.1–0.6), `momentum.min_adr_pct`
(3.0–7.0), `breakout.max_flag_depth` (0.3–0.6), `breakout.max_gap_pct`
(0.02–0.08).

The objective is **total R**, not R per trade, so a filter that improves
the average by removing profitable trades is not tightened. Adjustments
live in `learning_overrides.yaml`, are listed with their evidence on the
Learning page, auto-revert when the evidence fades, and can be reset.
`llm_enabled` [on] adds an AI post-mortem per closed trade (never required).

---

## 11. The Saturday insider scan (research, not trading)

Separate from the trading loop. The week's market-wide Unusual Whales flow
alerts (unusual preset; premium ≥ `insider_scan.min_premium` [$150 k],
volume/OI ≥ [3], DTE 3–45, ≥ 5 % OTM, ≥ 70 % at the ask, puts included,
market cap $100 M–$30 B, index/ETF tickers excluded) are aggregated per
ticker and scored 0–10 on: premium (log), volume vs OI, how far OTM
(10–35 % is the sweet spot), days to expiry, aggressiveness, one-directional
conviction, concentration in one strike/expiry, fresh OI, that week's
option volume vs its 30-day average, a mega-cap penalty, expiry before the
next earnings, **repeat buying across days**, a **quiet-stock backdrop**
(penalised if the stock already moved toward the bet), and **strikes beyond
the 52-week range**. Tickers ≥ `min_flag_score` [5.5] (top `max_flagged`
[15]) are flagged; with `ai_enabled` [on] each goes to the configured model
for a strict-JSON catalyst analysis. The following weeks score how each flag
actually played out (10-session outcome scorecard). Nothing here places an
order.

---

## 12. One trade, end to end

1. **Sunday** the universe is rebuilt: 2,860 common stocks ≥ $3 with ≥ $5 M
   a day.
2. **Monday 16:20** nightly scan. XYZ is up 62 % in three months, ADR 5.1 %,
   in the "AI infrastructure" industry theme (78th percentile, 71 % of
   members above their 20-day). It has a 17-day flag, 31 % deep, range
   contracting to 70 % of the flag's ADR, closing 2.4 % below the pivot at
   $48.10. QQQ is above its 20-day and breadth is 54 %: regime OK. Watch
   signal → armed. Context is gathered: news +0.2, edge +0.31 on 80 %
   coverage, earnings in 19 days. Plan: entry $48.20, stop $45.75 (1 ADR),
   $100 k × 0.5 % ÷ $2.45 = **204 shares** ($9.8 k, 9.8 % of equity), partial
   target $53.10. All checks pass; in `confirmed` mode nothing rests.
3. **Tuesday 09:50** focused pass. XYZ prints $48.35 on volume pacing to
   1.6× — but the last price is $48.05, back under the pivot: **HOLD**
   (wick-only), stays armed.
4. **Tuesday 10:05** price $48.60, holding above $48.20, pace 1.7×, 35
   minutes in, heat 2.1 % + 0.5 % under the 4 % cap, 5 of 8 slots used, one
   other AI-infrastructure name open (theme cap 3). **BUY 204 @ market**,
   filled $48.62; stop $45.75 rests at the broker.
5. **Wednesday** close $47.90 — above the failed-breakout floor ($47.86), no
   action.
6. **Friday** (day 3) close $51.40 > entry: **partial 67 shares** at the
   close, stop → $48.62 breakeven.
7. **Next Thursday** with the partial done, no 2 R limit rests any more; the
   remaining 137 shares trail the 10-day MA. Close $52.10 < MA $52.40 →
   **EXIT**, about +1.3 R on the whole trade. Journal entry written with every feature;
   the Saturday review counts it in the "pace 1.5–2.0×" and "theme ≥ 0.7"
   buckets.

---

## 13. Which setting to touch for what

| You want… | Change |
| --- | --- |
| Fewer, stronger names | raise `momentum.min_adr_pct`, `momentum.min_gain_*`, `themes.min_theme_percentile`, `edge.threshold` |
| More candidates | lower the same; widen `breakout.max_flag_depth` to 0.6; `schedule.arming_distance_pct` 0.08 |
| Smaller / bigger bets | `risk.risk_per_trade_pct` (0.005 = 0.5 %); `risk.max_position_pct`; `adaptive.*` |
| Fewer positions at once | `risk.max_positions`, `risk.max_portfolio_heat_pct`, `risk.max_positions_per_theme` |
| Stop trading on a bad day | `risk.daily_loss_limit_pct` (0.03 = −3 %) |
| Never miss a fast breakout | `entry.mode: resting` (accept more false breakouts) |
| Fewer false breakouts | keep `confirmed`; raise `entry.confirm_volume_ratio` to 1.3–1.5; `opening_range_minutes` 15–30 |
| Sit out choppy markets | `regime.min_breadth` 0.5; set `regime.max_vix: 30` |
| Take profits sooner / later | `management.partial_target_r`, `partial_after_days`, `trail_ma` (10 fast, 20 slow) |
| Cut dead trades faster | `management.time_stop_days` 3; `entry.failed_breakout_tolerance_pct` |
| Trust the options data more | `edge.gate` on with a higher `threshold`; `options_flow.require_bullish: true` (then missing flow **blocks**) |
| Spend less on paid calls | lower `context.max_symbols_per_cycle`, `edge.max_symbols_per_cycle`, `options_flow.max_symbols_per_cycle`; zero some `edge.weights` |
| Let the AI veto | `committee.enabled` + `can_veto`; or `reviewer.mode: gate` with `fail_closed: true` |
| Stop the learner from moving knobs | `learning.auto_apply: false` (lessons still written) |

Everything above is editable on the settings page (Quick settings for the
common ones, All strategy parameters for the rest, with the default shown
next to anything you changed), or by asking the Advisor page in plain
English — its proposals are validated the same way and applied only when
you accept them.
