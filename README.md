# xq-r1-001

Minimal public CI execution harness. Private source/data are fetched only on ephemeral hosted runners and are never stored in this repository.

Public logs intentionally suppress raw command, pytest, dataset, prediction and diagnostic content. They may expose only sanitized audit evidence needed to verify execution:

- exact private source SHA;
- per-step PASS/FAIL;
- pytest aggregate counts: passed, failed, skipped, xfailed, xpassed, deselected and errors;
- plan-declared numeric evaluation metrics (`int` / finite `float` only);
- plan-declared SHA-256 identities for immutable evaluation inputs/outputs.

A pytest command that returns success but has no recoverable aggregate summary is treated as an audit failure. A plan-declared numeric audit report that is missing, malformed, contains undeclared fields/types, non-finite numbers, or non-SHA strings is also treated as a CI failure.

Raw test names, paths, business text, Gold contents, predictions, per-sample errors and arbitrary strings must not be emitted into public logs.
