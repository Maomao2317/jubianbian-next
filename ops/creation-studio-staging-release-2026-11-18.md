# 创作工作台测试环境发布记录

## 范围

- 基线提交：`9370a0d8932409e97df4bdefe811569af645c4f2`
- 基线测试 build：`translation-name-protection-20261118-9370a0d`
- 发布内容：测试环境工作台首页、全部任务筛选、语言卡片选择，以及任务列表向 UI 提供类型筛选所需的原/生成语言标识。
- 不改识别/翻译/导出业务逻辑，不部署正式环境。
- 发布脚本只接受 `prepare SHA` 与 `deploy SHA`；检查前后生产容器指纹一致。

## 验证和发布

本地运行全部 `web-staging/test-*.cjs`、JavaScript 语法检查、Python 发布脚本安全测试和 `git diff --check`。完成代码审查并将验证后的版本推送到 `codex/multilingual-performance-quality` 后，在应用服务器执行：

```bash
python3 release_creation_studio.py prepare <完整已推送SHA>
python3 release_creation_studio.py deploy <完整已推送SHA>
```

准备阶段要求当前测试容器/build 与前端文件散列匹配基线，创建不依赖网络的派生镜像，并在无网络、隔离容器中运行完整后端回归。部署阶段要求准备记录和实时指纹未变、无活动识别任务；只替换测试 app 容器，保留数据挂载、端口与环境配置。健康/build、容器和 HTTP 静态文件散列验证通过后记录结果。自动回滚保留原镜像及源码备份。

生产前后指纹记录在服务器私有目录的 `staging-prepared.json` 和 `staging-deployment.json`，不包含用户任务或凭据并且不提交至 Git。
