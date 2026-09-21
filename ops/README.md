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
