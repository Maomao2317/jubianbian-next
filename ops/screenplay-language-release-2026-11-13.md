# Screenplay Language Staging Release

## Version comparison

At 2026-11-14, the staging container was observed at build
`screenplay-language-20261113-8a22e0c` from commit
`8a22e0c97c2d102ec8b99441658b9df06ab73aab`. The active staging compose also
keeps the prior Double11 carousel-removal overlay. The runtime build label is
not a substitute for comparing environment configuration or source trees.

| Area | Staging (test) | Production |
| --- | --- | --- |
| Runtime identity at observation | `jubianbian-staging`, build `screenplay-language-20261113-8a22e0c`, label `staging` | Production is only fingerprint-checked for no change |
| Network | port `18000` | loopback port `8000`, served externally by production Caddy |
| CORS | test origins | production origins |
| Default provider | GLM before this release; Ark in this release | Not changed |
| Data/config | staging data volume and its own server-side `fangzhou.env` | separate production data volume and its own server-side `fangzhou.env` |

The staging release explicitly switches the test container to `JBB_VIDEO_PROVIDER=ark`
because its `fangzhou.env` contains `ARK_API_KEY` but no `GLM_API_KEY`. Production
remains unchanged and continues to use its existing configuration.

The Git source must also be distinguished from the runtime labels. This
incremental release is based on the observed staging commit above. In addition
to the already-reviewed Ark output-limit and translation batching work on this
branch, it removes the user review gate: completed screenplays and historical
`review` tasks with screenplay content are downloadable immediately, review
hints appear at the bottom of the detail page, and the user review buttons are
not rendered. Generated clothing descriptions are limited to one or two core
garments or colors; dialogue and all non-clothing action, emotion, scene,
sound, and plot descriptions remain complete.

The staging branch intentionally retains the bilingual feature for test
verification. This staging-only release does not define or perform a
production promotion. A later production release must be prepared separately
from the reviewed no-language source and must not copy the bilingual staging
files into production.

No private values or business data are part of this release.

## Release scope

This release deploys only to `jubianbian-staging` and has no production command.
The release guard requires the live staging build and affected runtime file
hashes to match the baseline above, accepts an already-active screenplay
language overlay, and accepts only the explicit backend, frontend, test, and
release-support files. It builds an offline image overlay, runs the backend
regression suite in an isolated container, refuses active compute or local
uploads, and verifies the exact app image, all affected runtime file hashes,
environment, ports, mounts, and unchanged production runtime after the switch.
It pins the staging overlay against the automatic `main` deployment until a
later reviewed merge.

The pushed source commit and the resulting health/build, file hashes, task
counts, and runtime fingerprints are recorded in the host's
`/opt/jubianbian-screenplay-language-20261113-<SHA7>/staging-deployment.json`.
