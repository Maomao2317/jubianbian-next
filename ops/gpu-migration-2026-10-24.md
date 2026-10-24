# Featurize GPU migration, 2026-10-24

Verified source: `2de0d23ceee8769334d53c2795167c3ef6c3b707`, fetched from
`origin/codex/repair-batch-content` on the application server before deployment.
The source archive SHA-256 was
`391106715406c2599de2435e96d15917d312a2509c27835f9dca1ab434c9c23b`;
the destination archive matched.

The new worker uses RTX 4090, driver 580.95.05, Python 3.10.22,
Paddle GPU 3.4.0, PaddleOCR 3.4.0, and the existing Whisper small model.
The existing 7,327,825,920-byte dependency/model bundle passed SHA-256
verification before extraction and offline installation. Installation passed
`pip check`, a real Paddle CUDA operation, and GPU ASR warmup.

The worker root is `/home/featurize/work/jbb-ocr`. Its separate Python and
FFmpeg environment is `/home/featurize/work/jbb-python310`; the explicit Conda
package list is saved in `work/jbb-migration/conda-explicit.txt`. The image's
apt database could not be parsed, so FFmpeg was installed in this independent
environment. FFmpeg 9 rejected the existing `-vsync` option during the first
smoke run. FFmpeg 7.1.1 passed a decoding check and both final smoke runs.
The worker's private environment fixes PATH to the compatible tools.

The published verifier `ops/verify_gpu_asr.py` ran in an independent process
inside each existing application container, with all paid API keys cleared.
The server-local sample duration was verified with ffprobe as 199.273991
seconds. Staging passed through a temporary private SSH forward before the
normal shared GPU tunnel was connected. Both environments then passed
authenticated health checks through their existing routes, and production
passed the complete verifier through its normal route.

| Check | Staging | Production |
| --- | ---: | ---: |
| Concurrent GPU OCR/ASR seconds | 23.724 | 30.089 |
| GPU ASR items | 158 | 158 |
| GPU OCR items | 339 | 339 |
| Sampled frames / OCR pages | 720 / 901 | 720 / 901 |
| Complete sampling | true | true |
| CPU fallback ASR items | 165 | 165 |
| CPU fallback seconds | 30.166 | 28.109 |
| Additional paid calls | 0 | 0 |

The ASR provider was `faster-whisper-gpu`, OCR was `paddleocr-gpu`, and an
intentionally invalid ASR token produced the real `faster-whisper:small`
fallback. These are isolated evidence checks, not an end-to-end screenplay
generation benchmark.

The existing restricted tunnel identity was rotated for the new worker while
retaining the forced command, restriction flags, and exact loopback port
limits. The worker listens on GPU loopback port 8866 and reconnects its SSH
reverse forward to application-server loopback port 18866. The original
Docker-gateway proxies on 18867 serve both application containers. Host keys
were checked against the respective machines. Private configuration and keys
have mode 600 and remain outside Git.

Both original application container IDs and image IDs were unchanged, so
their mounts, environment, ports, and running processes were preserved.
Both `/api/health` responses were `ok`, still reporting
`video-reliability-20261023-r2`. The temporary staging forward on 18869 and
its SSH control connection were closed after final verification; the normal
reverse tunnel remained listening. Server and tunnel guards run detached
from the browser. After an instance reboot, use the existing manager's
`start` and `status` commands with the worker's explicit `--root` and
Python 3.10 environment.

Private before-state, key rollback material, failed-run diagnostic log,
staging/production results, and the final `verification.json` are saved on
the application server under
`/opt/jubianbian-gpu-migration-20261024/`. The final record reports
`verified=true` at `2026-10-24T03:54:54Z` (11:54:54 Asia/Shanghai).
The old GPU rental was not returned or deleted by this migration.
