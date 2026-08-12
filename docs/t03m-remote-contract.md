# Remote model-evaluation contract

The `t03m` target is the remote-only model-evaluation continuation for the registered Shared source SHA.

It must:

1. fetch the exact private source commit with `KNOWLEDGE_READ_TOKEN`;
2. restore only the registered private Actions artifact, bound by artifact id, source run id, repository id and SHA-256 digest;
3. run the committed evaluator without modifying private source/Gold/sample selection;
4. keep model stdout/stderr, Gold, manifest contents and predictions runner-local;
5. require the expected comparison outputs and 20-line Lightweight/Full prediction files;
6. publish only plan-declared aggregate numeric metrics plus SHA-256 identities;
7. cleanup all private source, restored input, predictions and logs under `RUNNER_TEMP`.

`t03` deterministic/full-regression evidence and `t03m` model-comparison evidence together satisfy the automated execution portion of the original Shared workflow. Reviewer approval remains separate.
