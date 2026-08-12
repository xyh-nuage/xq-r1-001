# Audit output contract

Raw private command output is never published. The runner exposes only safe aggregate evidence.

Pytest steps are detected automatically and must yield a parseable terminal summary. Public output includes only aggregate counts.

Evaluation steps may declare an audit block in the public plan:

```json
{
  "argv": ["python", "path/to/evaluator.py", "--audit-json", ".ci/audit.json"],
  "audit": {
    "type": "numeric_json",
    "path": ".ci/audit.json",
    "fields": {
      "gold_count": "int",
      "prediction_count": "int",
      "tp": "int",
      "fp": "int",
      "fn": "int",
      "precision": "float",
      "recall": "float",
      "f1": "float",
      "invalid_count": "int",
      "gold_sha256": "sha256",
      "prediction_sha256": "sha256",
      "evaluator_sha256": "sha256"
    }
  }
}
```

The private evaluator writes a flat JSON object at the declared path. The runner emits only declared values after strict type validation. Arbitrary strings, per-sample data and diagnostic text are never permitted through this channel.
