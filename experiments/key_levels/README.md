# Key-level context experiment

Offline ETH long analysis; no live configuration changes. Read PROTOCOL.md for fixed rules,
REPORT.zh-CN.md for conclusions, manifest.json for coverage and SHA256.

Python 3.12+ standard library only:

```
python experiments/key_levels/run.py
python -m unittest discover -s experiments/key_levels -p 'test_run.py' -v
```

The compressed inputs.jsonl.gz contains the exact minimized public-market inputs needed
for replay, including original stored live scores, data-quality flags, past price returns,
timestamps and provenance labels. It is a recovered subset, not a complete DB export.
The stopped Sample B exports and seven-day Railway log retention supply different pieces;
this does not resume, amend, or relabel the original Sample B.

Outputs: summary.csv (all groups and chronological halves), events.csv (all entry/exit
records and support context), diagnostics.json (unknown context, rejections and missed
winners), manifest.json (input identity and gaps). Costs are unlevered proxy costs;
sampled MAE/MFE do not capture all intraminute extrema. No stops or profit targets.
The original event subset with all required level information is separately reported
to distinguish data-availability selection from position-based selection.
