# 剧编编 · 视频转剧本

前端位于 `web-staging/`，后端位于 `backend/`。后端会同时托管前端页面，提供视频上传、任务处理、结构化剧本生成和 Markdown/TXT 导出。

## 本地开发

```powershell
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

打开 http://127.0.0.1:8000/ 。VSCode 可直接使用 `.vscode/launch.json` 启动后端。

方舟视频识别配置放在项目根目录的 `fangzhou.env`，VSCode 会自动读取它。真实 Key 只放在这个文件中，不要提交到 Git：

```env
ARK_API_KEY=你的方舟APIKey
ARK_API_KEY_2=你的第二个方舟APIKey（可选）
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=doubao-seed-2-1-turbo-260628
ARK_LITE_MODEL=doubao-seed-2-0-lite-260428
ARK_TURBO_MODEL=doubao-seed-2-1-turbo-260628
ARK_ROUTING_MODE=complexity
ARK_VIDEO_FPS=1.0
ARK_FILE_POLL_SECONDS=1
ARK_RESPONSE_POLL_TIMEOUT_SECONDS=900
ARK_RESPONSE_CREATE_ATTEMPTS=2
ARK_THINKING_TYPE=disabled
ARK_FALLBACK_ON_ERROR=1
```

## Docker 部署

```powershell
Copy-Item .env.example .env
docker compose --env-file fangzhou.env up -d --build
```

正式环境和测试环境使用独立 Compose 项目、容器和数据卷。正式环境通过
`https://jubianbian.com/` 提供服务；测试环境通过独立的
`https://test.jubianbian.com/` 访问，不会重启或覆盖正式环境：

```powershell
docker compose --env-file fangzhou.env -p jubianbian-staging \
  -f docker-compose.staging.yml up -d --build
```

测试地址也可以继续使用 `http://服务器IP:18000/` 进行底层排查。两个环境可以共用方舟 Key，但会共享方舟额度；
DNSPod 中将 `@`、`www`、`test` 三条 A 记录指向服务器公网 IP，Caddy 会分别为正式域名和测试子域名申请 HTTPS 证书。

当前 Compose 同时启动正式应用和 Caddy 反向代理。正式域名和测试子域名都由 Caddy 自动申请和续期 HTTPS 证书；正式应用的 `8000` 端口仅绑定本机，公网只保留 `80/443`，测试容器仍使用独立的 `18000` 端口。

应用内置了单进程限流和上传体积校验：普通 API 默认每个来源 IP 每分钟
120 次，创建任务默认每 10 分钟 10 次，上传上限由 `JBB_MAX_UPLOAD_MB`
控制。它适合当前单机测试环境，多实例时应迁移到 Redis 或反向代理限流。

`jbb-data` volume 持久化 SQLite 数据和上传文件。生产环境建议在服务前配置 HTTPS 反向代理，并按实际域名、鉴权和额度策略补齐访问控制。

## 自动部署、任务状态与监控

测试环境可由服务器端定时器自动跟随 GitHub `main` 分支：每 2 分钟检查一次新提交，自动同步代码、重建测试容器并执行健康检查；正式环境不会被该流程触碰。首次在服务器执行：

```bash
sudo /opt/jubianbian-staging/ops/install-staging-automation.sh
```

应用会把请求耗时、请求 ID、任务阶段、方舟/备用服务耗时、失败原因写入 `data/logs/app.log`，并通过容器标准输出保留。`/api/health` 返回环境、运行时长和活动任务数；`/api/metrics` 返回任务计数、提供商配置状态和日志大小；`/api/tasks/{id}` 会附带任务事件时间线，另有 `/api/tasks/{id}/events` 可单独查询。

`ops/backup_data.sh` 和 `ops/cleanup_data.sh` 用于定时备份与清理数据，
详见 `ops/README.md`。备份应复制到服务器之外的对象存储或另一台机器。

账号注册使用腾讯云 SES 事务邮件发送 6 位邮箱验证码。部署时在 `.env` 配置
`TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`、`TENCENTCLOUD_REGION` 和
已在腾讯云 SES 校验通过的 `TENCENTCLOUD_SES_FROM_EMAIL`；本地没有腾讯云密钥时可
保留 `JBB_AUTH_ALLOW_DEV_CODE=1`，接口会返回开发验证码用于联调，正式环境请设为 `0`。

配置 `ARK_API_KEY` 后，任务会优先走“上传视频 → 方舟 Responses API → 结构化剧本 → Markdown/TXT 导出”的真实链路。方舟返回限流、额度耗尽或暂时不可用时，默认（`ARK_FALLBACK_ON_ERROR=1`）自动尝试 OpenAI；备用识别也失败时任务进入重试/失败流程，不生成未经证实的本地伪剧本，并在识别概况中显示降级提示；需要严格暴露方舟故障时可将该开关设为 `0`。

`ARK_ROUTING_MODE=complexity` 时，系统会在调用方舟前根据视频时长、独立转写/OCR证据和关键帧可用性选择 Lite 或 Turbo，并把实际模型与路由原因写入任务质量记录；设为 `lite_only` 或 `turbo_only` 可用于对照测试。

批量上传使用“创建批次 → 每集独立上传”的两阶段协议，浏览器默认同时上传 4 集；任意一集上传完成即可进入识别，不需要等待整个 50–100 集批次。若配置下面的 S3 兼容参数，浏览器会通过预签名 URL 直传对象存储，应用服务器不再中转上传流量；未配置时自动使用本地数据卷，不影响现有部署：

```env
JBB_OBJECT_STORAGE_ENDPOINT=https://你的-S3-兼容端点
JBB_OBJECT_STORAGE_BUCKET=jubianbian
JBB_OBJECT_STORAGE_LEGACY_BUCKET=
JBB_OBJECT_STORAGE_REGION=auto
JBB_OBJECT_STORAGE_ACCESS_KEY=
JBB_OBJECT_STORAGE_SECRET_KEY=
JBB_OBJECT_STORAGE_ADDRESSING_STYLE=path
JBB_OBJECT_STORAGE_PREFIX=jubianbian/uploads
```

对象存储桶需允许站点域名执行 `PUT`，并放行 `Content-Type` 请求头。密钥只配置在服务端环境变量中，浏览器只获得有时效、仅能上传指定对象的预签名 URL。

### 大视频自动处理

用户上传上限仍为 500 MiB / 6 分钟，不将方舟 URL 输入链路遇到的 50 MiB 限制转嫁给用户。默认 45 MiB 以内不转码；对象存储视频小于 45 MiB 可直接用签名 URL。更大的原片生成识别副本，默认目标不超过 40 MiB，并按时长分配视频码率；横竖屏保持比例、不放大小尺寸画面，默认长边不超过 1280、视频码率最高 2500k、音频 96k，不主动降低帧率。副本优先存入私有对象存储，让方舟通过单文件临时签名读取；链接下载失败、对象存储不可用或 `ARK_VIDEO_SOURCE_MODE=files` 时才使用 Files 直传。ASR/OCR 始终读原片，不受副本压缩影响。

压缩结果须非空、比原片小且不超过目标大小，上传前另检查 45 MiB 安全预算；这只是本系统保守预算，不代表 Files API 与 URL 输入的接口限制完全相同。缺少 ffmpeg、压缩超时/失败或副本仍超标时，不会把超预算原片提交方舟，不再自动重跑同一次无效压缩，错误提示说明原片已保留。任务检查点以原文件大小、ETag/修改时间、采集及压缩配置隔离；重试复用已成功的语音、字幕和压缩副本，失败的采集源单独重采，前集人物上下文每次重新读取。检查点不保存 API Key 或签名链接，复核会重新签名。成功后释放本地压缩副本；失败任务本地副本超过 24 小时在后续任务启动时清理（保护活跃任务），远程副本随原任务的正常保留策略清理。

Files 默认单次等待 120 秒、最多 2 次，累计等待由 `ARK_UPLOAD_TOTAL_TIMEOUT_SECONDS=240` 限制；即使旧环境仍指定 240 秒/3 次，累计预算仍生效。上传重试耗尽不再触发整任务重新排队。页面显示“识别视频上传重试中 / 采集语音和字幕中”，视频准备失败不会提前进入画面识别阶段。

方舟读取可访问的视频链接也可能间歇性返回 `HTTP 400 / InvalidParameter / Timeout while processing video_url`。此类读取超时先刷新同一私有副本的签名并重试一次（`ARK_VIDEO_URL_ATTEMPTS=2`，最多可设 3 次），持续失败才进入 Files 直传，避免一次短暂读取故障立即变成长时间上传。权限、格式和模型配置错误不会触发这项重试。页面显示“正在重新读取识别视频”，日志记录重试次数但不输出签名链接；原片采集、压缩画质、帧率和局部复核保持原配置。

原片上传后，任务列表及详情显示“等待服务器压缩 / 服务器压缩中 X% / 提交识别视频”，批次显示压缩及等待集数。压缩百分比读取 ffmpeg 实际输出时间，至多每秒写入一次数据库；副本校验通过才记为 100%，与任务总进度分开。刷新或离开页面不影响服务器处理，原片仍保留；小视频不会显示压缩阶段。

`JBB_ARK_PROXY_MAX_IN_FLIGHT=1` 限制单进程同时转码数，`ARK_UPLOAD_PROXY_THREADS=2` 限制编解码线程，与上传和模型并发独立。日志记录压缩队列等待、转码耗时及前后文件大小，实际速度/画质需用代表性视频实测。其他参数见 `.env.example`。**已有 `fangzhou.env` 会覆盖新默认值**：部署时将旧 `ARK_UPLOAD_PROXY_MAX_MB=4`、`MAX_HEIGHT=540`、`VIDEO_BITRATE=400k`、`AUDIO_BITRATE=32k` 同步为示例中的 40、1280、2500k、96k，并补齐触发阈值、并发和线程配置。`MAX_MB=0` 可关闭压缩，但超安全预算视频会明确拒绝发送，不会绕过检查。

## 独立证据与质量复核

多视频任务在已有任务运行时仍每秒检查上传完成的队列，及时补满 `JBB_WORKER_CONCURRENCY`（默认 16）允许的空闲名额。`JBB_ARK_MAX_IN_FLIGHT` 默认同时允许 16 路模型请求；两项配置均支持 1–16，超过 16 时截到 16。正式与测试 Compose 使用相同默认值；已有 `fangzhou.env` 中的旧值会覆盖默认值，启用 16 路时需将这两项均设为 16，并重启后端或重建应用容器。

语音与 OCR 使用独立的有界采集队列，`JBB_EVIDENCE_CONCURRENCY`（默认 2）分别限制两种来源的并发；慢语音不再占住后续集的字幕采集名额。本地 ASR 默认单路、4 CPU 线程（`JBB_LOCAL_ASR_CONCURRENCY=1`、`JBB_LOCAL_ASR_CPU_THREADS=4`），可设置双路做对照，模型、beam、VAD 和词时间戳不变。证据协调与模型生成各有独立线程池，给视频准备及上传保留通用线程；初稿、自动复核及备用文本生成共用模型线程池。下载默认 2 路，Files/识别副本上传默认 2 路，转码默认 1 路，解码默认 2 路，OCR 子进程合计默认 4 路（每进程单线程），分别控制本地资源。

队列按用户当前占用名额公平补位，同一用户保持就绪顺序；两名用户都就绪时可各用 8 路，单人可用满 16 路。新用户到来后优先取得释放的名额，不中断已运行任务。

正式与测试 Compose 挂载同一个 `jubianbian-runtime-limits` 本地卷；方舟生成在所有已更新容器间合计最多 `JBB_SHARED_MODEL_CONCURRENCY=16` 路，断流后的后台轮询仍占用名额，进程退出时系统自动释放锁。下载、上传、转码、ASR、OCR 也共享资源名额。两个容器必须同时更新、挂载相同资源目录并使用一致上限；只更新一边不能约束旧容器。该机制针对同机本地磁盘，不支持多主机或网络文件系统。

`JBB_ARK_QUOTA_SCOPE` 区分账户额度组，默认保守地合并两个 Key。`JBB_ARK_QUOTA_RPM/TPM` 应填写方舟已激活的真实配额，默认 0 表示未指定；TPM 使用 `JBB_ARK_INPUT_TOKEN_RESERVE` 加输出预算做保守预留，结束后按实际 usage 回填，不能当作精确分词器。429 冷却在两个环境间共享，按 Retry-After（缺失时 30 秒）等待；有限重试复用已准备视频。16 路是允许的上限，实际吞吐取决于证据准备、硬件与账户配额。

实测使用 `ops/benchmark_batch.py`（Linux 应用镜像内运行），分别选择 `asr`、`evidence`、`batch` 模式，输出必须是不存在的新目录，避免打开线上数据库。`batch` 显式传 `--allow-paid-models` 后运行实际任务、复核及质量门禁；可用相同证据的 `--evidence-from` 对比 8/12/16 路模型吞吐，但报告会标记缓存复用，不能作为完整冷启动耗时。

清单中无 `batchId` 的样本独立运行；仅同一部剧的关联视频填写相同 `batchId`，并用 `batchIndex` / `batchTotal` 保留真实集序及总集数。不同原始批次不得为方便压测合并，否则会污染跨集人物上下文。报告记录关联批次数和独立样本数。

用 `python ops/summarize_benchmark.py /bench/batch8 /bench/batch12 /bench/batch16` 可只读汇总状态、耗时、内存、质量指标及模型操作计数；`--details` 展示逐集覆盖率和P0数量，不输出原片地址、剧本正文或密钥。日志里的429计数是匹配到的事件数，不代表服务商账户的全部限流请求。

2026-10-14 在8核/16GB服务器上用相同4个原视频对照，ASR单路、每路4线程用时30.641秒，双路、每路4线程用时32.820秒；完整文本与词时间戳的摘要逐集一致，峰值进程内存分别885.6MB和1213.1MB。因此该服务器保留单路ASR默认值，任务和模型仍支持16路。此结果只代表这批样本，其他硬件应重新测量。

同机16片完整证据采集耗时660.581秒；复用证据的首轮8/12/16路模型批次分别耗时493.977/325.645/732.354秒，失败1/0/3片，16路失败主要涉及视频读取及Files上传超时。首轮清单错误地合并了无关原批次，数据仅供吞吐、资源和故障观察，不能证明真实跨集人物质量或冷启动提速；已修正分组。按原始批次补测时遇到方舟欠费HTTP 403，测试已中止，尚无修正分组后的完整对照，不能认定16路最快或质量验收完成。

方舟明确返回账户欠费/余额不足时，错误会归一为 `account_balance_exhausted`，不再触发任务自动重试或视频重新上传；保留原片和成功采集的证据。现有备用识别策略不变；无可用备用链路时终止任务并按原流程只退款一次，已完成任务的复核失败则恢复旧版本。普通视频权限403、429限流与暂时故障仍按各自恢复策略处理。

任务证据的 `timingsMs.audioQueue` / `ocrQueue` 分别记录两种来源的排队耗时，`audio` / `ocr` 记录采集耗时。采样间隔、帧数、ASR 模型与 beam size、视频画质、模型路由及 P0 质量门禁保持现有配置；并发只调整调度，不省略任何证据或自动复核步骤。重试继续复用成功来源，采集异常时会等待所有原片读取结束后再释放临时视频。实际提速受 CPU、视频内容及模型服务耗时影响，应根据阶段日志在部署环境实测。

Docker 镜像内置 `faster-whisper`、Tesseract 简体中文语言包和 ffmpeg。默认用
`JBB_LOCAL_ASR_MODEL=small` 做独立音频转写，用
`JBB_OCR_SAMPLE_INTERVAL_SECONDS=0.25`（最多 720 帧，长视频受帧数上限约束）抽取字幕证据；密集采样字幕区域，另每秒扫描全屏姓名条和其他文字。连续相同字幕延长时间范围，空白后再出现的字幕保留为新片段；相同像素复用 OCR 结果，密集抽帧尽量只解码一次。ASR 保留短语音及字词时间锚点。首次运行会把 ASR 模型下载并缓存到持久化数据卷的 `models/huggingface` 目录。可按服务器性能调整设备、计算精度、语言、beam size 和 OCR 帧数，完整变量见 `.env.example`。已有环境变量会覆盖新默认值，需要自行同步。采样仍可能漏过短于间隔的字幕，实际识别质量和耗时需用原视频实测。

复杂度路由会在独立音频或 OCR 任一来源不可用时直接选择 Turbo。Lite 初稿只有遇到台词遗漏、人物归属、OS/VO 或事实一致性风险时才追加一次 Turbo 复核，并复用第一次上传得到的 `file_id`；动作拆分、人物外形、反应节奏等 P1 编辑建议不再触发人工复核。只有台词严重缺失/改写、说话人无法确认、正文臆造等 P0 内容风险会进入管理员复核。独立 ASR/OCR 缺失时覆盖率仍显示“未校验”，但不再仅因校验工具缺失阻断整集交付。

人物身份按同批视频的集序累计，以角色首次确定的名字统一后续出场人物和说话人标签，台词、字幕及动作原文不做跨集称谓替换；关系称谓依靠明确别名/关系证据匹配，不能把父亲合并到儿子。动画、动物及其他非人角色同样建立稳定身份。说话归属结合连续声源和同期证据，切到听者不会改变一句话的归属；嘴未动不代表内心独白，场内画外对白、OS、旁白、电话和回忆声分别分类。局部复核会回看问题前后声源，只在原问题范围内补漏或修正，并检查复核后 ASR/OCR 覆盖是否下降。

管理员在后台打开“管理员复核”，确认原视频与剧本一致后点击“确认通过并开放下载”，任务由 `review` 变为 `done`，视频流程即结束；若仍有漏台词等问题，点击“退回并免费重试”，不会再次扣除普通用户额度。普通用户无权执行这两个操作。

已完成任务也可以由管理员点击“免费重新复核”。系统会先保存当前已交付版本，再运行新的 ASR/OCR 与 Lite/Turbo 质量链；新结果无论自动门禁是否通过都必须回到管理员复核。管理员可以确认新版本、继续免费重试，或点击“恢复上次已完成版本”。复核失败时系统会自动恢复旧版本，不会退款或重复扣费。
