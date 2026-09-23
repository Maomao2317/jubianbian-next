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
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=doubao-seed-2-0-lite-260428
ARK_VIDEO_FPS=0.5
ARK_FILE_POLL_SECONDS=1
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

配置 `ARK_API_KEY` 后，任务会优先走“上传视频 → 方舟 Responses API → 结构化剧本 → Markdown/TXT 导出”的真实链路。方舟返回限流、额度耗尽或暂时不可用时，默认（`ARK_FALLBACK_ON_ERROR=1`）自动尝试 OpenAI，仍不可用则使用本地兜底剧本完成上传、任务状态和导出闭环，并在识别概况中显示降级提示；需要严格暴露服务故障时可将该开关设为 `0`。
