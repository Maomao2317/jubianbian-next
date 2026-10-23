# Video reliability release, 2026-10-23

Build: `video-reliability-20261023-r1`.

Verified source: `1169ad1570ba8dc3a8c84c06b3302abb4bccd170`, pushed to
`codex/repair-batch-content` before deployment. Runtime scope is limited to
`resource_limits.py`, `providers.py`, `script.py`, `processing.py`, and
`task_store.py` under `backend/app`.

Recognition upload capacity waits and transport deadlines are separate. A
capacity timeout is classified separately from a transport timeout. Files
uploads reserve capacity once, and a failed object-storage upload can reuse
the recognition proxy through the existing Files fallback. Capacity timeouts
do not trigger another route and another queue wait. Existing upload limits,
concurrency, model routing, configuration, data mounts, and ports are retained.
No concurrency slots were added.

Character descriptions deduplicate complete profile facts within the relevant
character introduction, preserving actions, independent sentences, role names
inside clothing descriptions, references to other characters, and punctuation
inside facts. Spoken subtitles are recovered only with matching audio evidence
or explicit voice metadata; the code does not guess speakers or off-screen
status. Independent screen text remains screen text. The same normalization
applies to generation, historical reads, and exports without rewriting stored
historical responses.

Review covered upload ownership, timeout classification, fallback and retry
behavior, preservation of screenplay actions and metadata, character scope,
repeat normalization, and the guarded deployment and incident-recovery tools.
The complete local backend suite passed 358 tests with 8 skips. The built image
passed all 358 backend tests without skips in 26.751 seconds. Seven synthetic
historical-replay checks also passed.

Read-only historical replay covered 475 staging tasks (298 with evidence) and
1,243 production tasks (1,115 with evidence), in 43.16 and 81.71 seconds,
respectively. It exercised 43 staging and 54 production spoken-subtitle
recoveries, and changed 831 staging and 1,107 production action blocks while
checking action content, dialogue, metadata, structure, and stability of repeated
profile enrichment. Detector issue-count differences are not evidence that all
historical content defects have been repaired. Existing scene consolidation can
change text on a subsequent normalization; replay compares that behavior with
the baseline separately from the profile-enrichment stability checks.

Private test logs, aggregate replay results, deployment records, and rollback
material are retained at
`/opt/jbb-video-reliability-20261023-1169ad1570ba`. Environment backups are
`staging-backup` and `production-backup` there. Their contents must not enter Git.

Staging passed at 15:19:07 and production at 15:30:21 China Standard Time on
2026-10-23. Both use image
`sha256:c631624d7169da31a94f41e0302814aad3341152bb5c4af5797fc39b0024b0c5`.
Production records `sameImageAsStaging: true`. Each passed health, 37 source-file
hashes, static asset hashes, runtime smoke checks, and preservation of effective
configuration, data mounts, and ports. The retained capacity wait is 600 seconds
and the proxy-upload budget is 900 seconds.

The production idle gate first refused promotion while 38 tasks were active.
No files or containers had been replaced. A subsequent bounded read-only wait
observed the remaining work finish naturally, reaching zero active tasks at
15:30:16 before promotion. No business task was cancelled or interrupted for
deployment. Existing-volume Compose warnings were followed by successful
verification of the unchanged actual data mounts.

Public HTTPS health checks for `test.jubianbian.com`, `jubianbian.com`, and
`www.jubianbian.com` returned HTTP 200, the expected environment and this build
at 15:31:59 China Standard Time. Private deployment records contain the complete
source revision, image identity, timestamps, previous image, and rollback paths.
The synthetic release-preservation and incident-caption tests also passed;
the former needed a rerun outside the local sandbox to create temporary files.

These checks do not guarantee that third-party networks or model services will
never fail, and they are not a throughput or load benchmark.
