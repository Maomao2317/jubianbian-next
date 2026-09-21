# 生产数据维护

这些脚本在服务器上运行，不包含 API Key。

- `backup_data.sh`：把 Docker 数据卷中的 SQLite 和上传视频打包到 `/opt/jubianbian-backups`，默认保留 14 天。
- `cleanup_data.sh`：默认清理 30 天以前已经完成或失败的任务及孤立视频，不触碰排队中或进行中的任务。

建议由 root 定时运行：

```cron
17 3 * * * root /bin/bash /opt/jubianbian/ops/backup_data.sh >> /var/log/jubianbian-backup.log 2>&1
47 3 * * 0 root /bin/bash /opt/jubianbian/ops/cleanup_data.sh >> /var/log/jubianbian-cleanup.log 2>&1
```

备份目录应保持 `700` 权限，并定期复制到另一台机器或对象存储；服务器本地备份不能抵御整机故障。

## 测试环境自动部署与监控

服务器上的 `/opt/jubianbian-github-check` 是 GitHub 仓库副本。安装一次自动化服务后，服务器每 2 分钟检查 `main` 分支；发现新提交就只更新测试环境、重新构建测试容器，并验证 `http://127.0.0.1:18000/api/health`。正式环境不会被这个定时器重启或覆盖。

```bash
sudo /opt/jubianbian-staging/ops/install-staging-automation.sh
sudo systemctl status jubianbian-staging-deploy.timer jubianbian-staging-monitor.timer
sudo journalctl -u jubianbian-staging-deploy.service -n 50 --no-pager
sudo journalctl -u jubianbian-staging-monitor.service -n 50 --no-pager
```

监控定时器每 5 分钟检查健康接口和 `/api/metrics`。应用日志写入各自数据卷的 `data/logs/app.log`，同时输出到容器日志；日志按大小自动轮转。任务详情接口会返回任务事件时间线，便于定位卡在哪个阶段、耗时多少或为何失败。
