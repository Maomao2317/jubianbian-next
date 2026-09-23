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

当前处理器已完成完整任务生命周期和结构化剧本导出。配置根目录 `fangzhou.env` 中的 `ARK_API_KEY` 后，会将视频上传到方舟 Files API，再调用 Responses API 生成结构化剧本；方舟限流或暂时不可用时，默认自动尝试 OpenAI，仍不可用则生成可导出的本地兜底剧本，保证测试闭环不被额度问题卡住。前端契约保持不变。

## 腾讯云邮箱验证码

注册验证码使用腾讯云 SES 的 `SendEmail` 接口。腾讯云控制台需要先完成发信域名或发信地址验证，并把已验证的发信地址填写到 `TENCENTCLOUD_SES_FROM_EMAIL`；只配置 SecretId 和 SecretKey 不能发信。

部署环境至少需要配置：

```text
TENCENTCLOUD_SECRET_ID=腾讯云 SecretId
TENCENTCLOUD_SECRET_KEY=腾讯云 SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
TENCENTCLOUD_SES_ENDPOINT=ses.tencentcloudapi.com
TENCENTCLOUD_SES_FROM_EMAIL=已验证的发信地址
TENCENTCLOUD_SES_FROM_NAME=剧编编
JBB_AUTH_ALLOW_DEV_CODE=0
```

验证码默认 10 分钟有效，60 秒内不能重复发送，连续输错 5 次后当前验证码失效。只有本地没有配置 SES 时，才可以显式设置 `JBB_AUTH_ALLOW_DEV_CODE=1` 使用开发验证码；测试和生产环境应保持为 `0`。
