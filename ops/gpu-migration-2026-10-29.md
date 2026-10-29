# Suanjiayun GPU migration, 2026-10-29

The replacement RTX 4090 worker is running in Suanjiayun southwest A with
NVIDIA driver 580.105.08. Its root is `/root/sj-tmp/jbb-ocr`.
Worker source commit `2de0d23ceee8769334d53c2795167c3ef6c3b707` was already
pushed before deployment. Dependency checks, a real Paddle CUDA operation,
Whisper GPU warmup, and authenticated worker health checks passed.

The existing published verifier processed a server-local 199.273991-second
sample. Staging passed through a temporary private tunnel before production
was connected. Production then passed through its existing GPU route.

| Check | Staging | Production |
| --- | ---: | ---: |
| Concurrent GPU OCR/ASR seconds | 40.844 | 43.489 |
| GPU ASR items | 158 | 158 |
| GPU OCR items | 339 | 339 |
| Sampled frames / OCR pages | 720 / 901 | 720 / 901 |
| Complete sampling | true | true |
| CPU fallback ASR items | 165 | 165 |
| CPU fallback seconds | 27.686 | 28.946 |
| Additional paid calls | 0 | 0 |

Providers were `faster-whisper-gpu` and `paddleocr-gpu`. An intentionally
invalid ASR token verified the real `faster-whisper:small` CPU fallback.
These isolated OCR/ASR checks do not measure complete screenplay generation.

Packet loss on the application-server/GPU route initially caused a transfer
timeout. BBR resolved the measured throughput problem. The reviewed config
was pushed as `55ad481f2488d87970557b7475a3afe9710faa25`, fetched from Git on
the application server, checked against the successful staging setting,
then installed unchanged as `/etc/sysctl.d/99-jubianbian-gpu.conf`.
Applying that file alone confirmed the live value remained `bbr`.

Both original application container IDs, images, and mounts were preserved.
Both application `/api/health` responses reported `status=ok`. The existing
Docker gateway proxies on 18867 and production reverse tunnel on loopback
18866 remain in use. The temporary staging SSH connection and its gateway
proxy on 18869 were closed after verification.

The worker and SSH guards run independently of the browser and restart
failed child processes. After an instance reboot, run the existing manager's
`start` and `status` commands using `/root/sj-tmp/jbb-ocr/venv/bin/python`
and explicit `--root /root/sj-tmp/jbb-ocr`.

Private rollback material, before-state, staging/production results, network
verification, and final `verification.json` are on the application server
under `/opt/jubianbian-gpu-migration-20261029/`. Final verification reported
`verified=true` at `2026-10-29T05:08:30.214847+00:00`. Credentials and business
data are excluded from this report and Git.
