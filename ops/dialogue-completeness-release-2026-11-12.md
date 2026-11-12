# Dialogue Completeness Release

Baseline: `4331a97c975eb3b2dfcddf9c957acaa77ac29dc3`, live build
`double11-carousel-20261111-4331a97`.

## Scope

- Never suppress different subtitle words merely because dialogue overlaps.
- Recover audio-supported captions as speech without guessing the speaker.
- Treat supported missing dialogue as blocking; review full captions with
  explicit speaking actions even when independent ASR is incomplete.
- Permit one normal content review when the initial model was already Turbo.
- Separate corrective-call budget from the normal reporting margin. Allow
  repair up to estimated break-even after 80% revenue realization, a 5%
  infrastructure reserve and a 1.25 call-estimate safety factor. This is an
  estimate, not a guarantee of actual profit. Attempt limits remain active.
- Preserve unresolved automatic findings when a user accepts a draft.
- Do not batch-reprocess historical tasks or alter credits/customer records.

## Checks And Deployment

Review and run backend regressions, all frontend test scripts, release safety
tests and `git diff --check`. Commit only source, tests and this runbook; push
the reviewed revision to `codex/script-translation-language` before deployment.

Run `release_dialogue_completeness.py prepare staging SHA` and `prepare
production SHA` from the same pushed revision, alongside the existing
`release_quality_ledger.py` and `release_gpu_transport.py` helper modules.
Preparation validates live baseline hashes, builds offline overlays and runs
isolated regressions without restarting services or attaching production data.

Deploy staging first, verify its health/build, file hashes and preserved
configuration, then deploy the same SHA to production. The helper refuses a
switch while compute, ready queued jobs or local uploads are active. Durable
direct-object-storage upload reservations survive a restart. The SQLite writer
lock prevents an old worker claiming jobs during the idle check and stop.

Deployment changes only the image/build and documented corrective-budget
environment keys, retaining mounts, ports, recognition services and existing
carousel assets. Staging remains pinned against the automatic main deployment.
Automatic rollback refuses to interrupt jobs claimed by the replacement.

The host records the pushed SHA, immutable image IDs, regression result,
before/after runtime fingerprints, health response, file hashes and verification
under `/opt/jubianbian-dialogue-completeness-20261112-SHORTSHA/` in
`staging-deployment.json` and `production-deployment.json`. Store a local
non-business verification summary outside Git after release.
