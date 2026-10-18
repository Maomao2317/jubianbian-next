"""Reusable Linux GPU worker setup; only stdlib is needed before installation.

Usage: python3 ops/manage_gpu_ocr.py --root /root/autodl-tmp/jbb-ocr install
See gpu-ocr-deployment.md. Secrets are read from root/service.env, never bundled.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from urllib.request import Request, urlopen

SOURCE_FILES = (
    "backend/app/__init__.py", "backend/app/config.py", "backend/app/evidence.py",
    "backend/app/remote_ocr.py", "backend/app/remote_asr.py", "backend/app/logging_setup.py", "backend/app/resource_limits.py",
    "ops/gpu_ocr_server.py", "ops/manage_gpu_ocr.py", "ops/gpu-ocr-deployment.md",
)


def environment(root):
    env = os.environ.copy()
    config = root / "service.env"
    if config.exists():
        if config.stat().st_mode & 0o077:
            raise RuntimeError("service.env must be private: chmod 600 service.env")
        for line in config.read_text().splitlines():
            line = line.strip().removeprefix("export ")
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values = shlex.split(value)
                env[key.strip()] = values[0] if values else ""
    env.update(PYTHONPATH=str(root / "source/backend"),
               JBB_DATA_DIR=str(root / "data"),
               PADDLE_PDX_CACHE_HOME=str(root / "cache/paddlex"),
               HF_HOME=str(root / "cache/huggingface"),
               MODELSCOPE_CACHE=str(root / "cache/modelscope"),
               PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True", OMP_NUM_THREADS="4")
    # The mirror may not support Xet's token exchange. Plain HTTP downloads
    # also work with the standard endpoint and reuse the same model cache.
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    if env.get("JBB_GPU_ASR_ENABLED") == "1":
        env.update(JBB_LOCAL_ASR_DEVICE="cuda", JBB_LOCAL_ASR_COMPUTE_TYPE="float16",
                   JBB_LOCAL_ASR_MODEL="small", JBB_LOCAL_ASR_CONCURRENCY="1",
                   JBB_LOCAL_ASR_MODEL_PATH=str(root / "cache/whisper-small"))
    libraries = sorted((root / "venv/lib/python3.10/site-packages/nvidia").glob("*/lib"))
    env["LD_LIBRARY_PATH"] = ":".join([*(str(path) for path in libraries), env.get("LD_LIBRARY_PATH", "")])
    return env


def run(args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def install(root):
    if platform.system() != "Linux" or platform.machine() != "x86_64" or sys.version_info[:2] != (3, 10):
        raise RuntimeError("Use Linux x86_64 / Python 3.10 to reuse this verified wheel cache")
    driver = subprocess.check_output(["nvidia-smi", "--query-gpu=driver_version,name", "--format=csv,noheader"], text=True)
    print(driver.strip(), flush=True)
    if int(driver.split(".", 1)[0]) < 575:
        raise RuntimeError("CUDA 12.9 requires NVIDIA driver 575 or newer; do not replace the host driver here")
    root.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent.parent
    for relative in SOURCE_FILES:
        target = root / "source" / relative
        if (source / relative).resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
    python = root / "venv/bin/python"
    if not python.exists():
        run([sys.executable, "-m", "venv", root / "venv"])
    wheels = root / "cache/wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    lock = root / "requirements.lock"
    pip = [python, "-m", "pip"]
    if lock.exists():
        # A migration cache is complete: fail clearly rather than downloading GB again.
        run([*pip, "install", "--no-index", "--find-links", wheels, "-r", lock])
    else:
        installed = subprocess.run([python, "-c", "import paddle; assert paddle.__version__ == '3.4.0'"], capture_output=True)
        if installed.returncode:
            # Fetch Paddle separately: its index does not contain every CUDA dependency.
            run([*pip, "download", "--no-deps", "paddlepaddle-gpu==3.4.0", "--dest", wheels,
                 "--index-url", "https://www.paddlepaddle.org.cn/packages/stable/cu129/"])
        run([*pip, "install", "--find-links", wheels, "paddlepaddle-gpu==3.4.0", "paddleocr==3.4.0",
             "fastapi", "uvicorn", "python-multipart", "pillow", "faster-whisper==1.2.1", "ctranslate2==4.8.2"])
    if not shutil.which("ffmpeg"):
        if os.geteuid() != 0:
            raise RuntimeError("Install ffmpeg with your system administrator, then rerun install")
        run(["apt-get", "update", "-qq"])
        run(["apt-get", "install", "-y", "-qq", "ffmpeg"])
    run([*pip, "check"])
    run([python, "-c", "import paddle; paddle.set_device('gpu:0'); print('CUDA_OK', paddle.ones([2,2]).numpy().tolist())"], env=environment(root))
    if environment(root).get("JBB_GPU_ASR_ENABLED") == "1":
        if not (root / "cache/whisper-small/model.bin").exists():
            run([python, "-c", "from huggingface_hub import snapshot_download; "
                 "snapshot_download('Systran/faster-whisper-small', local_dir="
                 + repr(str(root / "cache/whisper-small"))
                 + ", allow_patterns=['config.json','preprocessor_config.json','model.bin','tokenizer.json','vocabulary.*'])"],
                env=environment(root))
        run([python, "-c", "from ops.gpu_ocr_server import load_asr; load_asr(); print('ASR_CUDA_OK')"],
            cwd=root / "source", env=environment(root))
    if not lock.exists():
        lock.write_text(subprocess.check_output([*pip, "freeze"], text=True))
    print("INSTALL_OK", flush=True)


def command(root, name, env):
    if name == "server":
        return [str(root / "venv/bin/python"), "-m", "uvicorn", "ops.gpu_ocr_server:app",
                "--host", "127.0.0.1", "--port", "8866", "--workers", "1"]
    host = env.get("JBB_GPU_TUNNEL_HOST", "")
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise RuntimeError("Set JBB_GPU_TUNNEL_HOST=user@host in service.env")
    return ["ssh", "-N", "-T", "-i", str(root / "tunnel-key"),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "UserKnownHostsFile=" + str(root / "cloud-known-hosts"),
            "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-R", "127.0.0.1:18866:127.0.0.1:8866", host]


def guard(root, name):
    (root / "run").mkdir(exist_ok=True)
    with (root / "run" / (name + ".lock")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        env = environment(root)
        args = command(root, name, env)
        child = None
        stopping = False

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True
            if child and child.poll() is None:
                child.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        pid = root / "run" / (name + ".pid")
        pid.write_text(str(os.getpid()))
        try:
            while not stopping:
                child = subprocess.Popen(args, cwd=root / "source", env=env)
                code = child.wait()
                print(f"{name} exited {code}", flush=True)
                if not stopping:
                    time.sleep(3)
        finally:
            pid.unlink(missing_ok=True)


def start(root):
    env = environment(root)
    if len(env.get("JBB_GPU_OCR_TOKEN", "")) < 24:
        raise RuntimeError("Set a private JBB_GPU_OCR_TOKEN of at least 24 characters")
    names = ["server"] + (["tunnel"] if env.get("JBB_GPU_TUNNEL_HOST") else [])
    for name in names:
        command(root, name, env)  # Validate before detaching.
        with (root / (name + ".log")).open("ab") as log:
            subprocess.Popen([sys.executable, __file__, "--root", str(root), "guard", "--name", name],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    print("START_REQUESTED (safe to repeat); use status to verify readiness")


def status(root):
    env = environment(root)
    request = Request("http://127.0.0.1:8866/health", headers={"Authorization": "Bearer " + env.get("JBB_GPU_OCR_TOKEN", "")})
    with urlopen(request, timeout=5) as response:
        print(json.dumps(json.load(response), ensure_ascii=False))


def stop(root):
    for name in ("server", "tunnel"):
        pidfile = root / "run" / (name + ".pid")
        if pidfile.exists():
            pid = int(pidfile.read_text())
            try:
                # Do not signal an unrelated process after a reboot/PID reuse.
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                if "manage_gpu_ocr.py" in cmdline and str(root) in cmdline and "guard" in cmdline:
                    os.kill(pid, signal.SIGTERM)
            except FileNotFoundError:
                pass
    print("STOP_REQUESTED; wait for tunnel port release before starting a new GPU")


def bundle(root):
    lock = root / "requirements.lock"
    if not lock.exists():
        raise RuntimeError("Run install first to capture the verified dependency lock")
    wheels = root / "cache/wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    run([root / "venv/bin/python", "-m", "pip", "download", "--find-links", wheels,
         "-r", lock, "--dest", wheels])
    target = root / "gpu-ocr-migration.tar"
    with tarfile.open(target, "w") as archive:
        for relative in ("source", "requirements.lock", "cache"):
            archive.add(root / relative, arcname=relative)
    digest = hashlib.sha256()
    with target.open("rb") as file:
        for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    target.with_suffix(target.suffix + ".sha256").write_text(digest.hexdigest() + "  " + target.name + "\n")
    print(f"MIGRATION_BUNDLE {target} bytes={target.stat().st_size}; credentials and videos excluded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/root/autodl-tmp/jbb-ocr"))
    parser.add_argument("action", choices=("install", "start", "status", "stop", "bundle", "guard"))
    parser.add_argument("--name", choices=("server", "tunnel"))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "guard":
        if not args.name:
            parser.error("guard requires --name")
        guard(root, args.name)
    else:
        globals()[args.action](root)


if __name__ == "__main__":
    main()
