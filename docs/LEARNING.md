# Learning policy and incentives

The program adapts eight explicit selection/trigger thresholds. It does not fine-tune a model, perform reinforcement learning or update neural-network weights. Optional models explain/review decisions; their explanations are not causal evidence.

The original objective combines total R per week with marginal-band and rejected-setup comparisons. This can reward optimistic counterfactual fills, repeated testing and selection on the same history. Shadow outcomes also omit important portfolio and execution constraints.

## Rebuilt policy

1. Preserve entry features, decision gates, outcomes and evidence provenance.
2. Present buckets and counterfactuals as research hypotheses.
3. Default to `learning.auto_apply: false`.
4. With explicit opt-in, apply at most **one tightening** per review. The affected band needs at least 30 fresh broker-verified records, or the higher configured minimum, and its descriptive 95% mean interval's upper endpoint must be below the negative lift threshold.
5. Never automatically loosen or revert from shadows. Keep those as proposals.
6. Require fresh evidence following a previous adjustment; cooldown alone cannot make old trades new evidence.
7. Validate override keys, finite values and bounds. Keep explicit CLI precedence and a change history.
8. Do not increase base risk simply because recent trades won.

The interval is a descriptive normal approximation, not a performance guarantee. Trades can be correlated across dates/themes; thresholds are tested repeatedly; adapters do not reconcile every cost. Proposal-only operation remains the recommended workflow. Verified means observed execution price/quantity, not independently audited net returns.

Shadows are simplified stop/target/time-out simulations on real bars, not portfolio-feasible returns. They are never added to actual P&L. Paper results test mechanics, not live fill quality. Estimated/legacy records can suggest an investigation but cannot authorize changes.

## Credible improvement cycle

- Register one change and metric before evaluating it.
- Freeze data, universe, settings and execution assumptions.
- Compare baseline/candidate on disjoint forward periods, including costs, drawdown, turnover and exposure.
- Group observations by time/theme and use dependence-aware uncertainty estimates before making claims.
- Keep the final holdout untouched; a used holdout becomes development data.
- Observe the candidate in paper operation and inspect execution reconciliation.
- Record deployment and rollback criteria independently of the selection sample.

The repository provides mechanics and descriptive review. No completed holdout experiment demonstrates that the rebuilt strategy is profitable or delivers superior returns.
