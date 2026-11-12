# Bounded Dialogue Recovery Release

Baseline: `b8e87e2180ce3d942f5ea98dda5fc90914d33349`, live build
`dialogue-completeness-20261112-b8e87e2`.

## Scope

- Detect timed speech outside independent ASR coverage and clusters of
  sentence-length subtitles without corresponding speech blocks. Actions do
  not prove speech coverage; captions alone never prove audible dialogue.
- Retry only suspect clips using the existing private GPU ASR worker: at most
  three admitted calls and 72 seconds per task, persisted across task retries.
  Keep original evidence; cache success, empty output and failed admissions.
- Reject no-op/cosmetic reviews with unresolved blockers. Preserve the draft
  and unresolved findings instead of calling an unchanged result repaired.
- After an ineffective review, permit at most two local omission checks, each
  with a physical video envelope of at most 32 seconds. Require actual clips,
  forbid full-video fallback, and permit only one generation per local call.
- Budget physical clip duration, retain all paid usage, and preserve the
  existing estimated break-even guard and infrastructure reserve. No pricing,
  model configuration, GPU service, credentials or customer credit changes.

## Verification And Release

Run full backend regressions, focused release-safety tests and diff checks.
Review the source, then commit and push only the allowlisted source, tests and
this runbook to `codex/script-translation-language`. Do not commit artifacts,
credentials, customer scripts, production database rows or retained caches.

Use the pushed revision of `release_dialogue_completeness.py` alongside the
unchanged `release_quality_ledger.py` and `release_gpu_transport.py` helpers:

```text
python3 release_dialogue_completeness.py prepare staging SHA
python3 release_dialogue_completeness.py prepare production SHA
python3 release_dialogue_completeness.py deploy staging SHA
```

Preparation verifies baseline runtime hashes and budget settings, creates
offline image overlays and runs isolated regression tests without mounting
live data. Deployment preserves ports, mounts and unrelated configuration.
The idle/SQLite writer-lock guard refuses to interrupt active recognition.

Verify staging health, the exact pushed source hashes and a bounded replay of
retained problem evidence. Where media remains available, validate a suspect
short clip with the existing worker; do not rerun or replace customer tasks.
Record the actual checks and limitations, not an unmeasured zero-omission rate.
Then deploy the same revision:

```text
python3 release_dialogue_completeness.py deploy production SHA
```

The host records commit, images, file hashes, regression results, before/after
fingerprints and health under
`/opt/jubianbian-dialogue-recovery-20261112-SHORTSHA/`. Record a local non-business
verification summary outside Git. Existing tasks are not batch-reprocessed.
