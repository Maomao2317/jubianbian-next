# 2026-10-16 整剧上传失败排查与修复

## 当前状态

代码及测试环境配置已部署。用户已手动保存 CAM 策略版本 3 并设为当前版本；北京桶 CORS 也已保存。2026-10-16 09:00 左右（北京时间）的只读验证通过。整剧上传和识别实测由用户执行。

## 故障证据

- 2026-10-15 旧桶 `jubianbian-1312804418`（ap-seoul）删除后，测试环境仍使用旧桶配置。
- 当晚两次整剧上传各 67 集，共 134 个任务；批次接口成功，但全部进入 abort，没有 complete 请求，未进入识别。
- 两批合计扣除 420 积分，已全部退回；失败任务 `credits_used` 合计为 0。
- 北京桶 `jubianbian-beijing-1312804418`（ap-beijing）CORS 规则为空；正式、测试域名的 PUT 预检返回 403。
- 正式与测试服务使用同一现有上传账号凭据。对控制台确认存在的北京桶对象进行 HEAD / 限定 2 字节的 GET，均被拒绝。
- 修复前，CAM 子用户 `jbb_cos_uploader` 的 `JubianbianCOSUploads` 策略（ID 289286420，版本 2）仅授权旧首尔桶上传前缀。
- `/api/health` 当前只检查数据库，HTTP 200 不能证明对象存储可用。

## 已完成

- 迁移兼容提交 `46dffb3e94d5a1073b037e296a88500b54b513b4` 已推送 `main`。
- 测试配置备份：`/opt/jubianbian-staging/fangzhou.env.before-beijing-20261016`。
- 测试环境 endpoint、bucket、region 已改为北京，保留 legacy bucket 映射，继续使用 virtual addressing。
- `jubianbian-staging-deploy.service` 已成功部署该提交；测试公网健康返回 200，build 标识一致。
- 测试容器生成的上传地址主机名已指向 `jubianbian-beijing-1312804418.cos.ap-beijing.myqcloud.com`。
- 既有 `python -m unittest test_provider_delivery` 36 项通过（mock，不产生识别调用）。

## 已保存的云配置

### 应用账号策略

保持原有 PutObject、GetObject、HeadObject、DeleteObject 四项对象操作，仅将资源替换为：

```text
qcs::cos:ap-beijing:uid/1312804418:jubianbian-beijing-1312804418/jubianbian/uploads/*
```

当前版本 3 的 JSON 已核对，仅包含上述北京桶前缀和四项对象操作；关联账号仍为 `jbb_cos_uploader`。不授予 ListBucket 或桶管理权限，不更换密钥。历史版本 2 仍保留，但只授权已删除的首尔桶，回退不能恢复北京桶上传。

### 北京桶 CORS

- Origin：`https://jubianbian.com`、`https://www.jubianbian.com`、`https://test.jubianbian.com`。
- Methods：PUT；Allow-Headers：Content-Type。
- Expose-Headers：ETag、Content-Length、x-cos-request-id。
- Max-Age：600；启用 Vary: Origin。
- 保持桶私有读写。

## 保存后的验证结果

- 正式、测试容器对 `_healthcheck.txt`（2 字节）及现有历史视频（6,035,618 字节）的 HEAD 均返回 200；限定读取前 2 字节返回 206。
- 两环境 `media_info` 均能把旧首尔桶 URI 映射到北京桶并读取正确的视频大小；生成的 PUT 地址主机及 `Content-Type: video/mp4` 正确。
- 正式、www、测试三个域名的 PUT / content-type OPTIONS 均返回 200；Allow-Origin 对应请求域名，Allow-Methods 为 PUT，Expose-Headers 和 Vary 正确。
- 使用旧桶 URI 生成的签名 GET，经真实 HTTPS 限定读取返回 206。
- 正式和测试公网健康均返回 200。正式 build 元数据仍为 `92e3d60…`，测试为 `46dffb3…`；两环境运行中的 storage.py、routes.py、api.js、config.js Git blob 哈希均与当前仓库一致，因此旧 build 标签不代表上传代码未更新。
- 两容器均 running，未发生 OOM，RestartCount 为 0；近 6 小时、最多 6,000 行容器日志汇总未发现 traceback、ERROR、存储拒绝、签名错误、超时或供应商额度信号。正式日志另有少量 401/404，测试无 HTTP 错误。
- 磁盘空闲约 251.6 GiB，使用率 10.6%；可用内存约 14,852,344 KiB。测试部署和监控 timer 均 active。
- 两环境均无 queued/running 任务；近 48 小时测试失败任务仅发现原先 134 个上传失败任务，正式无失败任务。数据库错误字段未发现余额错误；这不证明供应商当前余额或付费识别链路正常。

## 其余限制与待验收项

- 两环境上传签名均为 6 小时；测试上传预约 TTL 为 6 小时，正式为 2 小时。预约从整批创建时计时，正式环境超过 2 小时仍未上传完成的后续集会过期并退款。此次只记录差异，未修改正式环境或重启服务；它不是昨天全部上传立即失败的原因。
- 前端与后端限制一致：单集 500 MiB、6 分钟，一批最多 999 集；前端 4 路上传，每次 PUT 超时 10 分钟。这些是现有限制，不代表已经完成大批量真实带宽验收。
- 健康接口仅检查数据库，仍不能单独用于判断 COS 或识别供应商可用。
- 本轮未写入测试对象、未创建上传批次、未运行整剧上传或付费识别；实际 PUT / complete 与识别质量、速度由用户实测。

保存凭证：`outputs/beijing-upload-cors-saved.jpg`、`outputs/beijing-upload-policy-saved.jpg`。
