# Autonomous improvement and incentives

The first rebuild only constrained a rule-based threshold review. That did not satisfy the intended hands-off system. Version 0.3 adds actual coefficient fitting and an experiment controller. The original journal/AI review remains useful for explanations and hypotheses; while autonomy is enabled it cannot separately mutate settings.

## What learns

`outcome_model.py` fits a ridge-regression model predicting trade R from eight entry-time features: relative volume, ADR, flag depth, gap, stop distance, theme strength, edge score and signal score. Its coefficients, scaling, training cutoff, validation size, errors and source counts are persisted. Legacy estimated outcomes have weight zero; observed paper fills weight 0.5, shadows 0.15 and broker-verified live fills 1.0. These weights are explicit design choices, not scientifically calibrated probabilities. Unknown live fees remain labelled; model rewards may be provisional until reconciliation receives costs.

Training uses earlier resolved outcomes; trades overlapping the later validation cutoff are purged. Shadow outcomes cannot supply the later validation set. At least 80 records, 40 purged fitting rows and 20 later non-shadow rows are required. A candidate must reduce later prediction error by at least 5% against a constant-mean predictor. This criterion only nominates a model for prospective testing. It does not prove a trading edge.

When deployed, predicted R contributes to setup ranking and rejects predictions below -0.25 R. Deterministic strategy, data, portfolio and halt gates still apply. The model cannot increase absolute risk limits or change credentials. Language-model weights are not trained.

The parameter researcher can tighten or loosen daily-testable selection rules, initial ADR stop distance (0.5–2.0 times ADR), and initial profit target (1–5 R). Those ranges are implementation bounds, not recommended settings for a particular account. Entry-time stops/targets are stored with positions; a promotion does not retroactively rebuild their initial plan. The candidate budget rotates across the search space. Intraday confirmation/options thresholds are not selected from daily bars that cannot represent their inputs.

Regime gate/scale settings remain operator policy, outside the learner's search space. Daily research shifts both regime permission and its sizing factor by one session. Prospective accounts receive the same structured market readings as the main desk; per-setup EP and breadth factors are applied once when sizing each plan. Entry evidence records the actual compounded risk multiplier.

## Discovery → prospective trial → deployment

1. Use completed sessions only, with at least 300 historical days by default. Compare a bounded number of candidates on training data using return minus twice absolute drawdown and a minimum trade count.
2. Reserve a later holdout before inspecting its results. Only the training winner is compared against baseline on that holdout. Record its data hashes, configurations, metrics and dates. After a consumed holdout, wait for at least 20 additional sessions before another historical evaluation. The manifest detects revisions; it is not a full immutable market-data archive.
3. Register one challenger. Starting after discovery, replay each newly observed trading cycle into separate baseline and challenger local paper accounts. Both see the same observation clock and captured context. Missing challenger context or interrupted observations prevent promotion. There is no backfilling of those observations with future information.
4. Compare paired daily account returns, taking only the last observation per day. A five-session block bootstrap estimates a return-difference interval, adjusted for candidate count. Defaults require 20 forward sessions, 30 closed challenger trades, positive interval lower bound, at least 0.5 percentage points of total return improvement, and no more than 10% drawdown. Trials expire after 90 days by default. These heuristic statistical gates do not provide a formal guarantee under nonstationarity, repeated inspection or market dependence.
5. Promote automatically when enabled. Live trading starts the policy at 25% of its normal per-trade risk budget. The canary needs at least 20 completed broker-verified outcomes with reported costs and positive aggregate R to restore normal budget. Lack of cost evidence keeps it in canary; it is not counted as zero fees.
6. Continue the prospective comparison for 60 days after promotion and then archive it, allowing new discovery. Post-promotion underperformance/drawdown can restore the previous policy. An account-equity deployment guard continues between trials. Cash withdrawals can trigger this conservative guard; deposits/withdrawals are not performance-adjusted yet. Existing positions retain their initial stops/targets and remain managed after rollback.

Config changes invalidate an experiment. Policy IDs are attached to entry features. Paper/real/shadow outcomes retain distinct provenance. Every promotion and rollback is logged. All thresholds above are configurable within validation bounds; disabling auto-promotion retains experimentation without deployment.

## Why this is not labelled reinforcement learning

Optimizing parameters after observing rewards is not by itself a sequential RL implementation. Full RL would need a defined observation/action space, transition and reward model, off-policy evaluation, treatment of execution costs and a simulator whose exploitable errors do not become the strategy. A strategy that wins by exploiting an optimistic fill simulation has learned the wrong objective.

Threshold-only adaptation is insufficient for the user's intended system. Fitting weights is useful too, but more model complexity is not automatically better. The implemented hybrid makes the behavior auditable and gives later RL candidates a deployment/evaluation boundary. An RL policy is **not implemented in this release**. The intraday replay and broker execution records are useful foundations; a validated RL environment and constrained policy learner remain separate engineering/research work.

The distinction follows the treatment of learning from fixed datasets in [Levine et al., Offline Reinforcement Learning](https://arxiv.org/abs/2005.01643). The need to limit historical selection and reserve fresh evidence is motivated by [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). Neither source establishes that this program's strategy is profitable.

## Important limits

Shadow records require finite positive prices, a stop below the entry and any target above the entry. Invalid setups are not simulated. Existing invalid records are quarantined with their original results retained for diagnosis, and are excluded from outcome-model fitting and journal-review statistics. Validation runs both when resolving shadows and when reviewing them; model training also validates its input independently. No epsilon denominator turns an impossible stop into a huge return label.

The outcome model's evidence count is refreshed during eligible research runs even when parameter backtesting is waiting for 20 fresh holdout sessions. That refresh does not bypass candidate validation or authorize deployment. Shadow-only evidence cannot supply the later non-shadow validation outcomes required by the model.

The system learns associations, not reliable causal explanations for every win or loss. Survivorship bias, revised bars, changing market regimes, missing rejected alternatives and correlated positions can distort evaluation. Local prospective paper fills are not broker paper fills or real executable counterfactuals. Costs, latency, spread, queue priority and liquidity need richer data and broker validation. Model fitting and threshold optimization consume different evidence: historical backtests train/select rules; journal/paper/shadow/live records fit the outcome model. Backtest trade rows without recorded entry features are not silently imported into model training.
