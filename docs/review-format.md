# Price-only annotation snapshots

These are outcome annotations, independent of gamma/flow feature values. The first release supplies no finalized empirical explosion rubric. Define the rubric from complete clean price paths, then record all qualifying episodes and reviewed intervals.

```json
{
  "rubric": "price-only-onset-v1",
  "confirmation_sessions": 5,
  "reviewed_intervals": [{"start": "2025-01-02", "end": "2025-02-28"}],
  "episodes": [{"onset": "2025-01-16", "confirmation": "2025-01-21", "end": "2025-01-24"}],
  "note": "Example format only; these dates are not research labels"
}
```

The complete interval attestation means all potentially qualifying outcomes within the interval were reviewed, including failures and reversals. Example weeks alone do not certify completeness. Use real exchange sessions, not calendar holidays. Do not save this illustrative document as actual evidence.

`confirmation_sessions` is the rubric's maximum additional sessions needed after a candidate onset to classify it. It applies to positive and negative outcome maturation. Episode confirmation gives the date the particular event can be classified. The evaluator conservatively applies the global confirmation lag in addition to the maximum event confirmation date; this overpurges where event confirmation is later and never shortens a necessary window.

Intervals must contain full episode end and confirmation. Overlapping episodes must be grouped/resolved by the rubric. New snapshots preserve previous ones. Label intervals reaching a fold boundary are purged; active continuation dates are ineligible for the onset model.
