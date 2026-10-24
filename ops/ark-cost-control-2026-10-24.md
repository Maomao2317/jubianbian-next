# Ark cost-control release — 2026-10-24

This record contains release and aggregate validation facts only. It contains
no customer identifiers, screenplay text, credentials, or business rows.

## Change and release

- Runtime source: `e90d4e1a256abd8865cbe72e212df1cd6cf7f05f`.
- Release build: `ark-cost-control-20261024-r2`.
- The same image was prepared, verified in staging, and promoted to
  production. The deployment record contains the full immutable image digest.
- Staging and production backups are retained under
  `/opt/jbb-video-reliability-20261023-e90d4e1a256a/` on the server.
- The production idle gate refused one attempt while work was active; no task
  was cancelled or interrupted. A later retry observed zero active tasks and
  completed the promotion.

## What changed

The initial Ark pass remains at 2 FPS. Automatic review now selects source-
fidelity risks instead of every advisory detector, combines nearby issue
ranges, and physically reviews one bounded clip at the dense review rate.
Clip patches are rebased to the source timeline and validated against the
original indices; a paid clip failure never silently starts another full-video
review. Provider usage is persisted per call with an explicit estimate source.

## Verification

- The built Linux image passed the backend suite: 366 tests, 8 documented
  skips. Read-only historical replays confirmed screenplay/quality identity and
  preserved P0 review triggers for both retained datasets.
- The staging deployment verified 37 runtime source/static hashes, health,
  unchanged mounts and ports, preserved GPU routing, and upload budgets.
- Live staging comparison (one retained sample, validation-only):
  - baseline 2 FPS: 61.029 s, 57,612 tokens;
  - candidate 2 FPS: 51.558 s, 57,319 tokens;
  - transcript coverage was 98% for both and the blocking count was identical;
  - localized review: clip 24.52–45.20 s, 130.553 s, 12,815 tokens;
  - source-timeline validation passed and business data was unchanged.

The comparison is a bounded sample, not a universal latency or billing
guarantee. Token costs above are provider public-price estimates, not an Ark
invoice. A separate exploratory 0.5 FPS run was rejected because it introduced
new OS/VO blocking issues; a 1 FPS run passed one sample but was slower and is
not the deployed setting.
