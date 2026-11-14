# Screenplay Language Staging Release

## Version comparison

At 2026-11-14, the staging container was observed at build
`carousel-removal-20261113-5856c8e` from commit `5856c8e4f8d73713dddee4bb6796373691485e95`.
That baseline already contains the multilingual UI and the expired Double11
carousel removal. The runtime build label is not a substitute for comparing
environment configuration or source trees.

| Area | Staging (test) | Production |
| --- | --- | --- |
| Runtime identity at observation | `jubianbian-staging`, build `carousel-removal-20261113-5856c8e`, label `staging` | Production was not queried or changed |
| Network | port `18000` | loopback port `8000`, served externally by production Caddy |
| CORS | test origins | production origins |
| Default provider | GLM before this release; Ark in this release | Not changed |
| Data/config | staging data volume and its own server-side `fangzhou.env` | separate production data volume and its own server-side `fangzhou.env` |

The staging release explicitly switches the test container to `JBB_VIDEO_PROVIDER=ark`
because its `fangzhou.env` contains `ARK_API_KEY` but no `GLM_API_KEY`. Production
remains unchanged and continues to use its existing configuration.

The Git source must also be distinguished from the runtime labels. This
incremental release is based on the observed staging commit above and changes
only `web-staging/css/app.css` plus release-support files. It does not replace
the already-tested multilingual application files with another branch.

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
