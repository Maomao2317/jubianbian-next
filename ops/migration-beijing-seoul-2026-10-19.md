# 北京迁回首尔记录（2026-10-19）

正式与测试应用已在首尔恢复，公网三个 A 记录已于北京时间 11:53–11:55
改回 `43.155.134.30`。腾讯公共 DNS `119.29.29.29` 与阿里公共 DNS
`223.5.5.5` 实测三个域名均返回首尔 IP，普通公网 HTTPS 健康检查全部通过。
11:57 再次确认两应用 healthy、代理运行、监控服务退出码为 0。北京应用保持停止，旧入口通过
HTTPS 转发到首尔，避免 DNS 缓存期间产生两套写入。

## 版本与发布顺序

- 运行源码：`d858fda076b2b4648966109169676ce7ba4fdf46`，包含已推送的
  `9b15f50ac3d31852e66feb7287a3362dbd2fa0d4` 修复。
- 运行标识：`repair-recovery-20261019-r1`。
- 迁移工具：`ops/migrate-back-seoul.py`，先审查、语法检查、
  `git diff --check`，再提交并推送后执行。最终工具提交为
  `1c2ea987597265ba45f8af3cf0108b787a1e05a7`；首尔使用的
  `e9d6bfc` 版本与该提交的目标端构建、恢复、启动和入口切换动作相同，
  后续提交只修正北京冻结时的清单路径与重复冻结保护。
- 首尔离线构建镜像 `jubianbian-return:repair-recovery-20261019-r1`，
  基于原来已有的同一基础镜像，逐个核对发布源码 SHA-256。
- 先启动测试环境，核对健康、版本与最小真实模型请求，再启动正式环境。
  两环境均使用同一已验证镜像；随后启用首尔本地路由、验证直连 HTTPS、
  启用北京转发，最后修改 DNS。

## 数据与运行核验

| 项目 | 结果 |
| --- | --- |
| 冻结前任务 | 两环境均无 queued / running / uploading 任务 |
| 最终同步 | 1,487 个业务卷文件 SHA-256 与大小全部一致；7 个变更文件 |
| 正式数据库 | SQLite integrity_check 通过，迁移前后记录核对一致 |
| 测试数据库 | SQLite integrity_check 通过，迁移前后记录核对一致 |
| 后台回归 | 首尔镜像内禁网运行 290 项测试，全部通过，11.544 秒 |
| HTTPS | 主域名、www、test 证书校验及健康检查均通过 |
| 前端与鉴权 | 三个入口 HTML、JS、CSS 可读；匿名 /api/me 均为 401 |
| 环境隔离 | 正式 health 为 production，测试为 staging，独立数据卷 |
| 真实模型连接 | 测试最小 SSE 调用约 2.83 秒，正式约 1.57 秒，均返回 OK |
| 原有会话 | 正式与测试原有登录会话及历史任务均可访问 |
| 导出 | 测试历史任务真实 TXT 下载成功，6,110 字节 |
| 自动恢复 | Docker 已启用；两应用和代理 restart 策略均为 unless-stopped |
| 定时监控 | 首尔两个原有 timer 已恢复，monitor service Result=success / ExecMainStatus=0 |
| 发布固定 | 保留 .staging-deploy-hold，避免自动部署覆盖当前修复版本 |
| 北京旧主机 | 两应用退出码 0；部署/监控 timer 与临时传输服务均 inactive |
| 北京转发 | 北京本机 HTTPS 实测返回首尔 production 健康结果 |
| 磁盘 | 首尔根分区约 213GB 可用 |

## 域名与数据卷

| DNSPod 记录 | 当前值 | TTL | 保存时间（北京时间） |
| --- | --- | --- | --- |
| @ A | 43.155.134.30 | 600 | 2026-10-19 11:53:42 |
| www A | 43.155.134.30 | 600 | 2026-10-19 11:54:28 |
| test A | 43.155.134.30 | 600 | 2026-10-19 11:55:06 |

正式入口为 https://jubianbian.com/ 与 https://www.jubianbian.com/，
测试入口为 https://test.jubianbian.com/。原有 4 条邮件 MX/TXT 记录保留。
运营商缓存刷新时间可能不同，不能把公共 DNS 的检查视为所有用户缓存已刷新。

```text
jubianbian_jbb-data-seoul-20261019r2                 -> 正式 /app/data
jubianbian-staging_jbb-staging-data-seoul-20261019r2 -> 测试 /app/data
```

两环境 Compose 的卷名与默认镜像标签已固定为上述当前版本，重启现有服务：

```bash
sudo docker compose --env-file /opt/jubianbian/fangzhou.env \
  -p jubianbian -f /opt/jubianbian/docker-compose.yml up -d --no-build
sudo docker compose --env-file /opt/jubianbian-staging/fangzhou.env \
  -p jubianbian-staging -f /opt/jubianbian-staging/docker-compose.yml up -d --no-build
```

发布新代码仍须遵循仓库流程：核对、检查、提交并推送 Git，先测试环境验证，
再部署正式环境。不要执行 `down -v`，也不要把北京旧应用直接启动为第二个写入点。

## 备份与限制

首尔原目录保存在 `/opt/jbb-return-seoul-20261019/previous-opt/`，
原数据卷保留。北京最新冻结数据卷、原配置和迁移归档均保留，未删除实例。
业务数据与配置以加密归档传输，临时传输入口已移除，传输服务已停止。
服务端验证记录位于首尔 `/opt/jbb-return-seoul-20261019/`：
`data-verified.json`、`staging-verified.json`、`production-verified.json`、
`https-smoke-verified.json`。这些服务端目录可能包含敏感配置，不应提交 Git。

首尔开始接收写入后，回退北京必须先停止写入并同步最新数据，不能只修改 DNS。
北京旧入口保留转发，但部分网络此前出现北京 TLS 连接重置，仍需客户端刷新 DNS
后直接访问首尔，不能保证旧入口在所有网络均可达。

原 AutoDL GPU 已到期，当前两环境继续使用已验证的 CPU 识别备用方案，
ASR/OCR 远程地址为空；本次迁回未恢复 GPU，也未重新执行整批付费视频压力测试。
最小模型请求通过仅证明连接与流式调用可用，不能保证每集固定的识别耗时。

首尔实例 `lhins-1zrjr17g` 当天约 14:29 到期，用户负责续费；本次未执行
续费、购买或删除资源操作。续费未完成前，不应把当前健康视为到期后仍能持续服务。
