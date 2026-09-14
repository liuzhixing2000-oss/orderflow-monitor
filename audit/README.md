# Frozen Orderflow incremental-edge audit

Read [the Chinese report](REPORT.zh-CN.md) and [the fixed protocol](PROTOCOL.md).

This directory is offline-only. No production modules or Railway settings are modified. It contains public-market snapshots recovered through authorized Railway logs, with explicit incompleteness rather than a claimed SQLite backup.

## Run

Python 3.12; NumPy 2.3.5:

```bash
python -m pip install -r audit/requirements.txt
python audit/run_audit.py
python -m unittest discover -s audit -p 'test_*.py' -v
```

The runner verifies the canonical data SHA256, uses unmodified vendored replay code, extracts the three unchanged pure research model functions for same-input parity, and overwrites only files under `audit/results/`. No API keys, live connections, parameter search or app startup are involved.

## Files

- `evidence/recovered_sample_a.jsonl.gz`: deterministic gzip of canonical full snapshot records with source deployment/log timestamp. Includes nine no-price startup log records explicitly skipped like original storage.
- `evidence/manifest.json`: exact per-symbol original bounds and counts, recovered log counts, data SHA256, observed gaps. Effective stored-equivalent counts are in feature diagnostics (5,444 per symbol).
- `evidence/original_replay.jsonl`: original report log records; `v2_replay.jsonl`: subsequent growing-database report, retained for provenance but not used as the original reference.
- `results/events.csv`: every original threshold crossing at every horizon, payload hash, entry/exit time, score/input features, return and quality flags.
- `results/original_report_reconciliation.csv`: all 120 per-symbol original report cells, including disagreements.
- `results/aggregate_results.csv`: all symbols/sides/thresholds/horizons for full and unscaled ablation variants.
- `results/component_ablation_at_original_events.csv`: score and eligibility effects without shifting original event timestamps.
- `results/price_only_baselines.csv`, `matched_controls.csv`, `matched_differences.csv`, `matched_summary.json`: price comparisons, including unmatched signals and exclusions.
- `results/btc_beta_attribution.csv`: past-only beta estimates and statistical residuals, not executable hedge returns.
- `results/sensitivity.json`, `feature_diagnostics.json`, `run_manifest.json`: stability, missingness, parity and source hashes.

Raw retrieval pages are transient intermediates ignored by git; the deduplicated canonical gzip is the frozen reusable source. Only market-data payloads are included.

## Limits

The recovered subset lacks the first 66 stored rows per symbol. ETH long70 aggregates match the original at all four horizons; full-dataset equivalence is not established. Matches are retrospective diagnostics on an already explored sample. Component masking changes event counts, so it cannot isolate causal feature value. No formal confidence interval is reported from four days. Sample B is specified but not started by this package.
