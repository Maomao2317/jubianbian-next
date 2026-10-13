# 剧编编后端

这是一个可本地运行的视频上传 → 后台处理 → 剧本导出闭环。服务启动后也会托管 `web-staging` 前端。

## 启动

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

打开 http://127.0.0.1:8000/ 。数据和上传文件默认保存在项目根目录的 `data/`。

## Docker 部署

在项目根目录执行：

```powershell
Copy-Item .env.example .env
docker compose --env-file fangzhou.env up -d --build
```

服务仍然通过 http://127.0.0.1:8000/ 访问。上传文件和 SQLite 数据保存在 Docker volume `jbb-data`，容器重建不会丢失。生产环境建议在前面接 Nginx/Caddy 提供 HTTPS，并限制上传接口的访问范围。

配置根目录 `fangzhou.env` 中的 `ARK_API_KEY` 后，通过方舟 Responses API 生成结构化剧本。小于 45 MiB 的对象存储视频可使用单对象临时 GET 签名直读；大视频默认生成不超过 40 MiB 的临时识别副本，通过 Files API 提交，ASR/OCR 仍使用未修改的原视频。横竖屏等比缩放、不主动降低帧率，压缩并发默认 1、编解码线程 2；压缩失败或副本超标不会退回提交超预算原片。无法读取视频 URL 时回退到 Files API，初次生成和自动复核复用同一份上传。签名不会改变存储桶权限，也不会写入任务错误日志。完整配置及旧环境迁移见根目录 README 的“大视频自动处理”。

`ARK_VIDEO_SOURCE_MODE=auto` 启用对象存储直读，设为 `files` 可切回原上传方式。`ARK_MAX_OUTPUT_TOKENS=32768` 为长剧本和复核设置显式输出上限（不是目标长度）；输出省去空字段和重复台词副本，仍保留完整对白、字幕、动作、人物与情绪证据。两项配置在正式和测试 Compose 中保持一致。

生成使用 JSON Schema 约束非空场次和正文；有定位时间的复核只返回问题范围的修正，其他正文保持不动，无时间定位的问题仍使用整集复核。异常的截断或空稿会在创建请求次数上限内重试，复用同一视频。复核增加阻断问题或降低独立台词证据覆盖率/准确率时保留初稿，同时记录两次调用用量。后续集在首集结果可用时读取同批同用户的人物表辅助识别，完成后继续按首集名称对齐；并行识别不会等待首集，台词及画面字幕的原始称谓不因规范人名而改写。

方舟不可用时，只有显式启用备用策略且配置了 OpenAI 才尝试备用识别；备用识别也失败时，任务会报告失败，不会生成未经证实的兜底剧本。

## 腾讯云邮箱验证码

注册验证码使用腾讯云 SES 的 `SendEmail` 接口。腾讯云控制台需要先完成发信域名或发信地址验证，并把已验证的发信地址填写到 `TENCENTCLOUD_SES_FROM_EMAIL`；只配置 SecretId 和 SecretKey 不能发信。

部署环境至少需要配置：

```text
TENCENTCLOUD_SECRET_ID=腾讯云 SecretId
TENCENTCLOUD_SECRET_KEY=腾讯云 SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
TENCENTCLOUD_SES_ENDPOINT=ses.tencentcloudapi.com
TENCENTCLOUD_SES_FROM_EMAIL=已验证的发信地址
TENCENTCLOUD_SES_TEMPLATE_ID=已审核的验证码模板 ID
TENCENTCLOUD_SES_FROM_NAME=剧编编
JBB_AUTH_ALLOW_DEV_CODE=0
```

验证码默认 10 分钟有效，60 秒内不能重复发送，连续输错 5 次后当前验证码失效。只有本地没有配置 SES 时，才可以显式设置 `JBB_AUTH_ALLOW_DEV_CODE=1` 使用开发验证码；测试和生产环境应保持为 `0`。
