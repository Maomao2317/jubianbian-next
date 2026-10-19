# 首尔 → 北京迁移记录（2026-10-19）

正式与测试应用已切到北京，域名和数据保持原有隔离。业务迁移与功能验证已完成；GPU 恢复包已校验完整，首尔临时传输服务及入口已关闭。腾讯云备案控制台明确显示 `jubianbian.com` **未备案**，因此当前访问成功不能视为北京公网长期上线条件已经齐备。

## 访问入口与资源

| 项目 | 首尔（保留作迁移回溯） | 北京（当前业务） |
| --- | --- | --- |
| 实例 | `lhins-1zrjr17g` | `lhins-db7gmucs` |
| 公网 IP | `43.155.134.30` | `82.156.205.129` |
| 配置 | 8 核 / 16GB / 300GB | 8 核 / 16GB / 270GB |
| 公网带宽 | 45Mbps | 18Mbps |
| 到期 | 2026-10-19，具体时刻未确认 | 2026-11-18 19:35:47（北京时间） |

- 正式：<https://jubianbian.com/>、<https://www.jubianbian.com/>
- 测试：<https://test.jubianbian.com/>
- DNSPod 的 `@`、`www`、`test` 三条 A 记录已分别于北京时间 09:01:55、09:03:03、09:06:07 改为北京 IP；TTL 均为 600 秒。
- 其余 4 条邮件 MX/TXT 记录保留。阿里公共 DNS `223.5.5.5`、腾讯公共 DNS `119.29.29.29` 以及北京主机解析均已返回北京 IP。
- 北京实例已开放 TCP 80/443。正式应用仅绑定本机端口；测试端口维持原 Compose 设置。
- 首尔应用容器已停止，首尔 Caddy 把三个域名的请求通过 HTTPS 转发到北京。旧 DNS 缓存期间仍由北京单点处理业务写入；首尔实例到期后该过渡转发将无法继续。

## 已核验的数据与功能

| 检查 | 结果 |
| --- | --- |
| 最终同步 | 1,503 个文件逐个比对 SHA-256 / 大小完全一致；最终增量 8 个文件，删除 0 个 |
| 正式 SQLite | `PRAGMA integrity_check` 通过；6 个用户、328 个任务 |
| 测试 SQLite | `PRAGMA integrity_check` 通过；3 个用户、628 个任务 |
| 业务镜像 | 两环境均为 `sha256:7ad7fda761470885bee19e56c0055c0ed9564866bec78f84ed7d0bf524331952` |
| 版本 | `scene-consolidation-20261018-r1` |
| HTTPS / 前端 | 三个域名的证书校验、HTML、JS、CSS 和 `/api/health` 通过 |
| 环境隔离 | 正式 health 返回 `production`，测试返回 `staging`，数据卷独立 |
| 鉴权 / 会话 | 未登录 `/api/me` 返回 401；浏览器原有登录会话可以读取各自任务列表和积分 |
| 历史剧本 | 正式和测试任务详情完整显示；正式真实 MD 导出 2,591 字节、测试真实 TXT 导出 6,110 字节，文件内容包含对应剧本 |
| 方舟模型 | 两环境各完成一次最小真实 Responses 调用，模型 `doubao-seed-2-1-turbo-260628` |
| COS | 两环境私有对象写入/读取成功，已有视频下载成功；小型验证对象保留在 `_migration-check/20261019/` 下 |
| CPU 识别 | 两环境均用真实视频完成 faster-whisper small 和 tesseract 推理，返回非空语音/文字证据 |
| 标准部署 | 不带迁移覆盖文件的 Compose `up -d --no-build` 已验证，容器保持健康 |
| 自动启动 | Docker 已启用；三容器均为 `restart: unless-stopped` |
| 测试监控 | 定时器启用，手动运行成功检查 health / metrics |
| 收尾复核 | 北京时间 09:33，三个公网域名及首尔旧 IP 转发的 health 均正常；北京两应用健康，首尔两应用保持停止；归档下载服务退出码 0，北京磁盘剩余约 194GB |

以上是迁移与组件连通性验证，未重新执行完整的大批量视频生产压测，也未向用户邮箱发送验证邮件。

## 当前识别方式

旧 AutoDL GPU 已到期，且旧 GPU SSH 转发依赖首尔。因此北京两环境将 `JBB_ASR_REMOTE_URL`、`JBB_OCR_REMOTE_URL` 置空，使用已迁移并实际验证的本地 CPU 模型。当前服务运行不依赖首尔 GPU 隧道。

GPU 模型、配置和恢复资料已完整保存在北京，原始归档 SHA-256 与首尔一致；这不表示 GPU 服务已在北京恢复。CPU 识别性能及 tesseract 的结果不能据此宣称与原 GPU 方案等同。

## 后续部署和运维

北京保留以下目录：

```text
/opt/jubianbian
/opt/jubianbian-staging
/opt/jubianbian-github-check
/opt/jbb-migration-20261019
```

服务使用普通 Compose 和标准数据卷：

```bash
sudo docker compose --env-file /opt/jubianbian/fangzhou.env \
  -p jubianbian -f /opt/jubianbian/docker-compose.yml up -d --no-build
sudo docker compose --env-file /opt/jubianbian-staging/fangzhou.env \
  -p jubianbian-staging -f /opt/jubianbian-staging/docker-compose.yml up -d --no-build
```

`--no-build` 用于重启现有已验证镜像。发布代码时需按仓库现有发布流程构建和校验新镜像。

```text
jubianbian_jbb-data                     → 正式 /app/data
jubianbian-staging_jbb-staging-data      → 测试 /app/data
jubianbian-runtime-limits               → 两环境共享 /app/runtime-limits
jubianbian_caddy-data                   → Caddy /data（证书）
jubianbian_caddy-config                 → Caddy /config
```

这些数据卷由迁移程序创建，Compose 会提示“卷不是由 Compose 创建”；已验证可以正常复用。不要使用 `down -v` 清理业务卷。

测试环境的 `jubianbian-staging-deploy.timer` 和 `jubianbian-staging-monitor.timer` 均已启用，发布分支维持 `main`。保留 `.staging-deploy-hold` 固定当前已验证版本，自动部署按原逻辑跳过，监控照常运行。合并并核对对应源码版本后，才按原发布流程解除固定；本次没有解除。

北京已成功读取 GitHub `origin` 的 `refs/heads/main`，连通性检查退出码为 0。迁移没有触发一次新的代码发布。

常用检查：

```bash
sudo docker ps
sudo systemctl status jubianbian-staging-deploy.timer jubianbian-staging-monitor.timer
sudo journalctl -u jubianbian-staging-monitor.service -n 30 --no-pager
curl --fail https://jubianbian.com/api/health
curl --fail https://test.jubianbian.com/api/health
```

服务器原有 root crontab 已核对：仅有腾讯云代理维护任务，没有既存的业务备份计划；本次没有增加新的自动清理或删除计划。

## 备份与回退约束

北京 `/opt/jbb-migration-20261019` 为受限访问的迁移资料目录，包含初始快照、镜像、最终增量、源配置和验证标记。此目录含服务端敏感配置，不应公开或提交到 Git。

- `initial/` 与 `initial.tar.gz`：初始源码、镜像、在线 SQLite 备份和数据快照。
- `final/` 与 `final.tar.gz`：停止首尔写入后的最终增量及完整文件清单。
- `initial-docker-volumes/`：发布最终数据前保留的初始卷内容。
- 后缀 `-beijing-20261019` 的卷：经过最终清单校验的冻结副本；运行环境使用标准卷名。
- `final-data-verified.json`、`standard-deployment-verified.json`、`https-smoke-verified.json`：迁移验证记录。
- `gpu.tar.gz`：GPU 恢复原始归档，实际为未压缩 tar，7,327,825,920 字节；已校验 SHA-256 及 192 个归档成员，顶层为 `cache`、`requirements.lock`、`source`。
- `gpu/`：GPU 恢复资料的已解包副本；`gpu-recovery-verified.json` 为最终归档验证记录。
- 首尔原数据、源码、镜像和 `Caddyfile.original` 保留，没有删除实例或数据。

全部归档完成后，首尔的 `jbb-migration-transfer.service` 已停止（`inactive/dead`），Caddy 中临时归档传输路由已移除；三个业务域名的过渡转发继续保留。

加密初始包 SHA-256：

```text
5d7ac43938af628ae3b6aea50b869bd6c1b7f93f185fe8aeb9839c3ebdf01db4
```

加密最终增量包 SHA-256：

```text
b8e2fd9173adc7a3b72617a6949e06ffc74f4cbc4eaa0f6db22cc314e4c219d2
```

GPU 恢复原始归档 SHA-256：

```text
4878057dd45ef8221e25ca2fb139de3a8857d30d630713b497f20f5ed4ba089b
```

北京开始接收写入后，**不能仅把 DNS 改回首尔并启动旧应用**，否则会丢失新任务或造成数据分叉。需要回退时应先停止新任务进入、等待活动任务完成、冻结北京写入并备份；把北京最新数据库及新增文件同步到可用回退实例并校验后，再启动目标应用和切换入口。首尔已到期时不能把它视为可用回退主机，需在可用实例上恢复。

## 长期上线待办

2026-10-19 约 09:10，腾讯云「我的备案」页针对 `jubianbian.com` 显示“未备案”。当前 HTTPS 实测成功，但这不能保证后续北京公网服务不受备案要求影响。需要用户提供并确认备案主体、域名及网站材料，完成腾讯云 / 管局流程；迁移中未创建新的云角色、提交备案材料或购买/续费资源。

腾讯云[备案资源要求](https://cloud.tencent.com/document/product/243/18908)规定，轻量应用服务器需为中国大陆地域、包年包月、购买总时长至少 3 个月（含续费），备案期间剩余时长至少 1 个月，并有非零公网带宽。北京实例的累计购买时长未核实，不能仅凭到期日判定是否符合条件；办理前还需核对资源资格。

北京实例下次到期为 **2026-11-18 19:35:47**。本次未执行续费或购买操作。
