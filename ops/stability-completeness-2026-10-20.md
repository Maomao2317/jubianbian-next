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

Source commit, image digest, history comparison, deployment verification and
rollback locations will be recorded here after successful deployment.
