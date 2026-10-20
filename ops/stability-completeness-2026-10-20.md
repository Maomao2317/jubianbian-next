# Stability and completeness release, 2026-10-20

Release build: `stability-completeness-20261020-r1`.

This release fixes a reproduced completeness-check bypass: a repeated line in
one part of an episode could hide an omitted occurrence elsewhere. Timestamp
drift now uses one ordered alignment, with separate occurrence checks. Missing
content still enters the existing automatic repair/review flow. Manual review,
original video, cached evidence, previous screenplay versions and rollback
remain available. This does not establish the cause of every historical visual
or dialogue omission and does not guarantee that model output is always complete.

COS downloads and recognition-proxy uploads now run in a child transport process.
The parent ends and reaps a transfer after 300/90 seconds, respectively; capacity
queueing is limited to 600 seconds. Temporary downloads are cleaned up, and the
existing Files fallback reuses the same recognition proxy. These limits bound
individual transfers, not the total episode processing time or all provider calls.

GPU failure cooldown is shared across application processes and both Docker
environments. One probe checks recovery; other requests use the existing CPU
fallback. Healthy GPU calls remain concurrent. A late success cannot reset a
newer failure, and a bookkeeping error cannot discard a successful GPU result.
GPU recognition settings, sampling, evidence requirements and parallelism remain
unchanged. No infrastructure purchase or renewal is part of this release.

Review covered transfer termination/cleanup, credential isolation, retry and
fallback behavior, shared SQLite leases, repeated-dialogue detection, timestamp
drift, and preservation of the mobile-admin application baseline.

Local validation before commit: 305 backend tests passed (8 skipped), 3 frontend
test suites passed; after the bookkeeping fix, all 17 GPU tests passed. The
deployment tool reruns the complete backend suite offline inside the built image
and compares retained screenplay/evidence read-only against the baseline.

Deployment order: push source, prepare the image from that exact fetched commit,
verify staging, then promote that same image to production. Refuse active tasks;
preserve environment settings, data mounts and ports. Private rollback material
and aggregate audit records stay under `/opt/jbb-stability-20261020-<commit>`.

Verified source: `a2b769c63f14ef2acb21d15cead4cab5b6a7e1ce`, pushed to
`codex/repair-batch-content` before deployment. Runtime changes originate in
`628ceaaf7db88ca5bffd3864c63191b7fe5d5f59`; subsequent commits fix offline
image tagging and URL encoding of Chinese asset names in release verification.

Both environments use image
`sha256:f50761a6de8c126644f8c008ebfa7630418f4f0fde14bde52d3a1604f2143ecc`.
Staging passed at 08:48:46 and production at 08:55:50, China Standard Time
(2026-10-20). Each passed health, 37 runtime source-file hashes, static asset
hashes, configuration, data mount and port checks. Production used the verified
staging image. Both were idle before replacement. Three public HTTPS health
checks (`jubianbian.com`, `www.jubianbian.com`, `test.jubianbian.com`) returned
HTTP 200, the expected environment and this build after promotion. The image
passed all 306 backend tests offline, with no skips, in 21.448 seconds.

Read-only historical replay covered 3 staging and 334 production tasks with
retained evidence. Normalized screenplay content was unchanged for every task.
Staging quality results were unchanged. Production had 35 tasks with additional
issue counts and 34 with fewer counts: 52 dialogue and 1 visual-text suspicion
were added, while 51 visual-text and 1 dialogue suspicion were removed. These
are detector differences, not confirmed omissions or repaired historical text.
The displayed issue list is capped at 100 (3 staging and 297 production tasks
reached the cap), so changes to nonblocking issue counts can reflect displacement
at that cap. The comparison includes all blocking issues separately. Production
replay took 13.16 seconds; the slowest single quality check took 0.074 seconds.

Live staging validation used an existing 199.274-second sample, without creating
business tasks or enabling paid-model calls. The first run timed out on remote
ASR and exercised CPU fallback; OCR succeeded. This prevented promotion until a
second run passed. The second concurrent GPU run took 42.5 seconds, returning
161 ASR and 336 OCR items with complete sampling. Deliberately invalid ASR
authentication then exercised the real CPU fallback in 27.929 seconds, returning
165 items. An isolated invalid-token scope does not cool down the normal GPU
scope. The first timeout's network/service cause is not established by this test.

The same live check passed after production promotion: concurrent GPU ASR/OCR
took 47.597 seconds and returned 161/336 items with complete sampling. Invalid
authentication exercised CPU fallback in 28.817 seconds, returning 165 items.
Both checks disabled paid-model keys, reported zero additional paid calls, and
left business tasks and balances untouched. The existing GPU proxy service and
socket were active after validation. These are sample recognition timings, not
end-to-end screenplay-generation timings or a concurrency/load benchmark.

Private records, replay aggregates, backend test log, live-check results and
rollback material are retained at
`/opt/jbb-stability-20261020-a2b769c63f14`. Environment-specific backups are
`staging-backup` and `production-backup` there; do not commit their contents.
The previous image is
`sha256:b8be0b0315b1158a4670f9f375d775cf06209c44e3fe9216c7dd10e15c47354e`.
An earlier staging verification failure on an unencoded Chinese asset URL
automatically restored the old deployment before this corrected release was
prepared. No production rollback was needed.
