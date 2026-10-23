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

## Follow-up: Files transport budget

Guarded production recovery completed one previously failed original. A second
original failed again after object transport returned an I/O error and the
Files fallback exhausted its 240-second transport budget. The retained route
checkpoint selected Files on subsequent recovery. A successful object upload
of 36,815,955 bytes in the same window took 549.6 seconds. This is evidence
that a progressing upload can need longer than the old Files budget; it is
not evidence of a model-generation slot shortage.

The r2 follow-up aligns the Files total transfer/retry budget with the existing
900-second object-upload budget. The per-socket timeout remains 120 seconds,
admission remains 600 seconds, and concurrency, media bytes, compression,
model routing, content normalization, billing, and data mounts are preserved.
The additional runtime source change is limited to `backend/app/config.py`.
Compose defaults and the example environment match the new default; deployment
explicitly pins this one setting so an older effective override cannot retain
240 seconds.

Code review covered bounded retries, slow writes versus stalled writes,
configuration pinning, source-scope guards, and idle-only promotion. A simulated
549-second transfer reproduces failure under 240 seconds and succeeds with
900 seconds while preserving every byte and fixed-length framing. The local
359-test run had 8 skips and one Windows child-start timing failure; the failing
test plus all nine upload-budget tests passed on a focused rerun. Promotion
remained gated on the full backend suite in the built Linux image; the result
is recorded below. Deployment verification alone does not establish complete
account recovery.

The r2 source `24618e07ea2c34ae257425930748273543a089c0` was pushed and its
exact Git objects verified before the server fetched it. The built Linux image
passed all 359 tests without skips in 27.724 seconds; both read-only historical
replays passed. Staging passed deployment verification at 16:12:58 and
production at 16:15:56 China Standard Time on 2026-10-23. Both use image
`sha256:cca1146f300dc756adab4ebb2f3b94591c5a81f59c45397dff24480dcc8ca5a3`.
The production idle gate initially refused promotion with four active tasks;
the subsequent read-only wait observed zero before deployment. No active task
was cancelled or interrupted for this release.

Both deployments verified 37 runtime source/static file hashes, static HTTP
responses, health, the unchanged mounts and ports, and the visual-description
and content-reliability smoke checks. Effective configuration changed only
the build identifier and `ARK_UPLOAD_TOTAL_TIMEOUT_SECONDS=900`. Public HTTPS
health on the test, apex production, and www production domains returned
HTTP 200 and the expected r2 build and environment at 16:16:59 China Standard
Time. Private r2 verification and rollback records are retained at
`/opt/jbb-video-reliability-20261023-24618e07ea2c`.

Post-deployment recovery has not established complete incident resolution.
The first of three distinct originals reached `done`. The third reached
`review` at 16:46:25 China Standard Time after object upload exhausted its
budget and Files fallback succeeded. Both passed historical rendering and
Markdown export checks. Quality detectors still reported possible omissions
and screenplay-detail issues, including on the `done` result; successful
rendering and export do not establish content completeness.

The second original exhausted the r2 Files transfer budget after 900 seconds.
One further guarded recovery started at 16:49:52 China Standard Time only
after the global active-task count reached zero. It validated the fresh task
snapshot, failed status, absence of a result or pending recheck, original media
size and object ETag, and duplicate-media guards, and preserved billing. It
completed upload and reached content repair at 16:54:41. The existing bounded
automatic retry then ran once because content repair still found uncovered
video ranges. The task reached `review` at 17:01:31 China Standard Time with
a persisted result. Historical rendering and Markdown export passed. The
final observation at 17:01:58 found
zero active tasks, one `done` result and two `review` results across the three
distinct originals; none remained failed. No result-bearing original was
manually requeued. No unbounded retry loop was introduced.

All three results still carry quality-detector warnings, including suspected
missing dialogue and screenplay-detail issues. These are detector signals
requiring content review, not proof of missing content or evidence that all
content defects have been fixed. No completeness or future-failure guarantee
follows from successful task recovery. The private final observations and retry
classification are retained as `solo-recovery-final.json` and
`solo-recovery-final-audit.json` under the r2 release directory.

The retained proxies are about 35--38 MB. Admission for the affected transfers
took milliseconds, so increasing model slots is not supported by this evidence.
Read-only TCP inspection found retransmissions and low transfer rates while
CPU and memory were available. This narrows the failure to media delivery;
it does not yet establish a network-provider fault or a purchased-bandwidth
limit. No concurrency increase, network setting change, source recompression,
or interruption of other active tasks was made during diagnosis. Private
recovery plans and network samples remain on the server, outside Git.
