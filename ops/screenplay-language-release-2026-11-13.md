# Screenplay Language Staging Release

## Version comparison

At 2026-11-13 13:57 China Standard Time, the public health endpoints reported
`dialogue-recovery-20261112-8fc2337` for both environments. Their environment
labels were respectively `staging` and `production`. The runtime build label is
not a substitute for comparing environment configuration or source trees.

| Area | Staging (test) | Production |
| --- | --- | --- |
| Runtime identity at observation | `jubianbian-staging`, build `dialogue-recovery-20261112-8fc2337`, label `staging` | `jubianbian`, same build label, environment `production` |
| Network | port `18000` | loopback port `8000`, served externally by production Caddy |
| CORS | test origins | production origins |
| Default provider | GLM | Ark |
| Data/config | staging data volume and its own server-side `fangzhou.env` | separate production data volume and its own server-side `fangzhou.env` |

The staging release explicitly switches the test container to `JBB_VIDEO_PROVIDER=ark`
because its `fangzhou.env` contains `ARK_API_KEY` but no `GLM_API_KEY`. Production
remains unchanged and continues to use its existing configuration.

The Git source must also be distinguished from the runtime labels. The observed
staging baseline is commit `8fc2337b5b04b291f023dbe503d9f7b0901d1ae6`. The
multilingual feature commit `d547cb102f949656c5f16db0676e932e04bc80b4` is its
direct child and changes 20 application/test files (643 insertions, 99
deletions) relative to that baseline. At this comparison, `origin/main` pointed
to `46dffb3e94d5a1073b037e296a88500b54b513b4`; the cumulative tree difference
between it and the staging baseline is 164 files (22,324 insertions, 2,400
deletions). That cumulative branch difference is not a change count for the
single `origin/main` commit. This release therefore uses the verified staging
baseline and does not substitute the current `main` tree.

No private values or business data are part of this release.

## Release scope

This release deploys only to `jubianbian-staging` and has no production command.
The release guard requires the live staging build and affected runtime file
hashes to match the baseline above, and accepts only the explicit application,
test, and release-support files. It builds an offline image overlay, runs the
backend regression suite in an isolated container, refuses active compute or
local uploads, and verifies the exact app image, changed files, environment,
ports, mounts, and unchanged production runtime after the switch. It pins the
staging overlay against the automatic `main` deployment until a later reviewed
merge.

The pushed source commit and the resulting health/build, file hashes, task
counts, and runtime fingerprints are recorded in the host's
`/opt/jubianbian-screenplay-language-20261113-<SHA7>/staging-deployment.json`.
