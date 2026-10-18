# GPU OCR 配置与换机

GPU 语音可通过 `service.env` 的 `JBB_GPU_ASR_ENABLED=1` 启用。保留 Whisper small、beam=5、原短句 VAD 和逐词时间戳，CUDA float16 推理；`status` 同时检查 `asrReady=true`。应用端另设 `JBB_ASR_REMOTE_URL`（同 OCR 地址）、`JBB_ASR_REMOTE_TOKEN`（同私有 token）和60秒超时。无损16k单声道 PCM 传输，空结果、不完整响应、故障或超时回退原 CPU 识别。ASR 启动失败不会关闭 OCR。

small 模型保存在 `cache/whisper-small`，首次安装需要下载；可用 `HF_ENDPOINT` 指定镜像。依赖变化后在已验证环境更新 `requirements.lock`，再执行 `bundle`，将语音模型和新 wheel 一并纳入换机缓存。迁移后无需重复下载这些文件。

语音传输使用 gzip 级别1无损压缩，解压后严格核对原始采样数。样本156.502秒：CPU18.231秒、GPU端到端6.374秒（约2.86倍），GPU推理3.803秒、上传2.027秒。保持模型/beam/VAD设置；CPU int8 与 GPU float16 的转写不是逐字相同，样本规范化文本相似度98.92%，首尾0.79～155.04秒一致。整剧耗时仍受视频模型与自动补缺影响。

仅测试环境启用远端 OCR。服务只绑定 GPU 本机 127.0.0.1:8866，经反向 SSH 到应用服务器；不需要 AutoDL 公网服务或改正式环境。GPU 不可达、响应漏帧或超时会回退原 CPU OCR。保持原采样间隔、全屏姓名卡、字幕区域和空帧分隔规则。

## 当前已验证的基础环境

- Linux x86_64、Python 3.10、NVIDIA driver >=575，Paddle GPU 3.4.0 CUDA 12.9、PaddleOCR 3.4.0。
- 本次 RTX PRO 6000 Blackwell 96GB / driver 590.44.01 已通过真实 CUDA 算子验证。
- 每台新 GPU 仍需运行兼容性和实际 OCR 检查；Python/CUDA/架构不兼容时脚本停止，不替换宿主机驱动。
- 单 worker 常驻模型，检测 batch 8、跨帧文字识别 batch 64，按字图宽高比合批后恢复每页原顺序。保留官方检测与裁字参数，不降低采样密度。模型缓存位于数据盘 `cache/`；精确依赖版本记录在 `requirements.lock`。

## 首次安装

在仓库根目录运行（安装需要下载数 GB CUDA 运行库）：

```sh
python3 ops/manage_gpu_ocr.py --root /root/autodl-tmp/jbb-ocr install
```

在 `/root/autodl-tmp/jbb-ocr/service.env` 设置 `JBB_GPU_OCR_TOKEN`（至少24字符）及 `JBB_GPU_TUNNEL_HOST=ubuntu@应用服务器`，权限 600。私钥 `tunnel-key` 权限 600；`cloud-known-hosts` 保存经过核验的主机密钥，禁止关闭主机密钥检查。复用本次的受限隧道身份即可，不要把密钥提交到 Git。

```sh
python3 /root/autodl-tmp/jbb-ocr/source/ops/manage_gpu_ocr.py start
python3 /root/autodl-tmp/jbb-ocr/source/ops/manage_gpu_ocr.py status
```

`start` 可重复执行，进程锁阻止重复实例。后台进程负责故障重启，SSH 每15秒检查连接并自动重连。日志为 `server.log`、`tunnel.log`。`status` 必须返回 ready=true、provider=paddleocr-gpu。机器重启后需重新运行 start，进程重启机制不等于系统启动项。

## 下次换机

本次环境完成后运行 `bundle`，生成不含视频、token、私钥的依赖/模型迁移包和 SHA-256 校验文件；复制它到自己的持久存储，AutoDL 删除实例可能同时删除数据盘。当前账号尚未初始化 AutoDL 文件存储，不应把实例数据盘当成跨实例网盘。

```sh
python3 /root/autodl-tmp/jbb-ocr/source/ops/manage_gpu_ocr.py bundle
```

1. 旧机器执行 `stop`，确认旧 SSH 已释放端口，避免新旧 GPU 争用同一端口。
2. 新机器选择 Python 3.10 / Linux x86_64，复制 `gpu-ocr-migration.tar` 和 `.tar.sha256` 到数据盘，运行 `sha256sum -c gpu-ocr-migration.tar.sha256`，再解压到 `jbb-ocr/`。另行安全复制 `service.env`、`tunnel-key`、`cloud-known-hosts`，保持私有权限。
3. 运行包内 `source/ops/manage_gpu_ocr.py install`。有锁文件时仅从 wheel 缓存安装，不重新下载大包；虚拟环境在新机器重新创建，不能直接搬旧 venv。
4. 运行 `start`、`status`，再从应用容器验证授权 health 和一条旧视频端到端 OCR。确认完整帧数与 GPU provider 后开始用户测试。

迁移包可换目录，但每个命令都要加 `--root 新绝对目录`。不要在旧 GPU 尚运行时让新机器抢占隧道。若缓存缺少 wheel，安装会明确报错；在旧环境重新 bundle 补齐。

默认目录下的新机操作示例（迁移包与校验文件已放到 `/root/autodl-tmp`）：

```sh
cd /root/autodl-tmp
sha256sum -c gpu-ocr-migration.tar.sha256
mkdir -p jbb-ocr
tar -xf gpu-ocr-migration.tar -C jbb-ocr
python3 jbb-ocr/source/ops/manage_gpu_ocr.py install
# 安全复制三份连接文件后：
python3 jbb-ocr/source/ops/manage_gpu_ocr.py start
python3 jbb-ocr/source/ops/manage_gpu_ocr.py status
```

## 应用端与回退

测试容器设置 `JBB_OCR_REMOTE_URL=http://172.19.0.1:18867`、同一个 `JBB_OCR_REMOTE_TOKEN`、超时90秒。服务器代理只监听测试 Docker 网关，转发到反向 SSH loopback 18866。GPU 断线时自动 CPU 回退；主动回滚可清空远端 URL 并重建测试容器。

空稿在模型解析、任务保存、管理员放行与下载环节拦截；缺段仍走原有有界补缺和复核流程。GPU 只是 OCR 证据提速，不能保证视频模型永远不遗漏，因此不能把模型输出为空显示为任务成功。

本次测试部署使用 `.staging-deploy-hold` 暂时固定已验证的源码，避免自动同步 main 覆盖本次修复。合并这些改动并核对部署版本后再移除该文件恢复自动发布。正式环境不设置 GPU 参数。
