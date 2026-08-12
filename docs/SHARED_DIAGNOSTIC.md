# Shared Gold20 diagnostic target

`t03d` is an analysis-only target for `SHARED-LOCAL-SEMANTICS-EXTRACTION-001`.

It reuses the exact frozen private source SHA, reviewed Gold20 manifest artifact and committed Gold/evaluator, but runs only Shared/Lightweight local discovery. It does not execute Full downstream assessment or Full end-to-end extraction.

Detailed diagnostic outputs are encrypted before leaving the hosted runner. The public artifact contains ciphertext only. Public logs expose aggregate metrics and SHA-256 identities.
