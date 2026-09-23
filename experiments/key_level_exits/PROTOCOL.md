# Frozen exploratory exit experiment — 2026-09-23

Question: enter at a pre-existing ETH support without a score/reaction gate, then test whether stored model scores improve exits over price alone. This is a new exploratory test on previously inspected data, not out-of-sample validation. No parameter search. Use the exact 17,592 snapshots and cutoff from ../key_levels; preserve its coverage caveats. No live deployment or Sample B amendment.

Historical inputs have continuation long/short scores, not raw delta/OI/book series. Consequently the added exit is a composite-score proxy, not a pure order-flow ablation. The score itself contains price and regime information. Missing/unqualified scores never trigger discretionary exits.

Supports and ATR use the causal historical window definitions of ../key_levels/PROTOCOL.md. Entry: current qualified observed price is between support and support+0.25 ATR, previous qualified price was above the CURRENT band upper edge, and adjacent observations are <=90s apart. The band uses only information known now; rolling support changes may themselves change membership. No positive-return, trend or score gate. If several supports qualify, choose the highest support (closest below price), tie order prior_day, prior_1h, prior_4h. Single union strategy, no level selection by results.

Queue a market entry for the next qualified snapshot <=90s later; freeze support and ATR at detection. Cancel if next price is at/below the initial stop. Stop = support-0.5 ATR; 1R = actual next-snapshot entry minus stop. No score threshold at entry. Common 240min cooldown from each accepted entry, independent of policy exit; early exits do not admit extra trades. Initial stop never widens.

Three exits share exactly these entries, initial risk and a 240min maximum hold:
1. fixed: initial hard stop, target entry+2R, otherwise time exit.
2. price_trail: initial hard stop; once an observed price reaches entry+1R, ratchet stop to max(old stop, running observed peak-1R), effective from the NEXT snapshot; otherwise time exit. No fixed profit target.
3. score_trail: identical price_trail plus market exit at the next snapshot after TWO consecutive quality-eligible observations <=90s apart have short score > long score AND stored 5min price return <0. Both observations must be after entry. Next-snapshot execution prevents same-observation lookahead fills. No other score tuning.

Hard stops and targets are evaluated on observed minute prices and filled at the observed price, not ideal barrier prices; a stop can lose more than 1R. At each snapshot: stop, previously queued score exit, target/time, then compute future stop/score signals. All three are proxies, not tick-executable stop simulations. Intraminute missed barriers remain a material limitation.

Require a common full 4h price path using existing >=90% coverage, <=180s gap and <=180s late-exit constraints even for early-exit policies. This makes paired comparisons valid on one availability-selected cohort; report excluded entries. 0.12 percentage points round-trip cost on notional, converted into R separately for every entry. No leverage assumption or account sizing backtest. Report net percent and R, paired score-minus-price R, winner cutting and loser saving, marked-to-observed-price drawdown in cumulative R, first/second chronological halves and leave-best-entry-day-out. No claims of significance from small samples. Persist protocol, code, minimized scores, trades and summaries.
