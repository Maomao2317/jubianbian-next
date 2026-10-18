"""Private, warmed PaddleOCR worker. Run with backend/ on PYTHONPATH.

Bind to loopback behind SSH, set JBB_GPU_OCR_TOKEN, use one Uvicorn worker.
The exact CPU sampler and subtitle/name-card merging are shared with the app.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import zlib

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("OMP_NUM_THREADS", "4")
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from app.evidence import _ocr_evidence_local, _bounded_text
from app.config import OCR_SAMPLE_INTERVAL_SECONDS, OCR_MAX_FRAMES
from app.remote_ocr import PROTOCOL

TOKEN = os.environ.get("JBB_GPU_OCR_TOKEN", "")
MODEL = None
GPU_LOCK = threading.Lock()
ADMISSION = asyncio.Semaphore(2)
REC_BATCH = int(os.environ.get("JBB_GPU_OCR_REC_BATCH", "64"))
DET_BATCH = int(os.environ.get("JBB_GPU_OCR_DET_BATCH", "8"))
ASR_ENABLED = os.environ.get("JBB_GPU_ASR_ENABLED") == "1"
ASR_READY = False
ASR_ADMISSION = asyncio.Semaphore(2)


def load_asr():
    global ASR_READY
    import numpy as np
    from app import evidence
    from app import config
    if config.LOCAL_ASR_DEVICE != "cuda" or config.LOCAL_ASR_COMPUTE_TYPE != "float16":
        raise RuntimeError("GPU ASR requires CUDA float16")
    evidence._asr_items_from_audio(np.zeros(16000, dtype=np.float32), "gpu-warmup")
    ASR_READY = True


def load_model():
    global MODEL
    import paddle
    import numpy as np
    import cv2
    from paddleocr import TextDetection, TextRecognition
    from paddlex.inference.pipelines.components import CropByPolys, SortQuadBoxes
    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
        raise RuntimeError("A CUDA GPU is required; CPU must not masquerade as GPU OCR")
    paddle.set_device("gpu:0")
    cv2.setNumThreads(4)
    detector = TextDetection(model_name="PP-OCRv5_server_det", device="gpu:0")
    recognizer = TextRecognition(model_name="PP-OCRv5_server_rec", device="gpu:0")
    detector.predict(np.full((160, 320, 3), 255, dtype=np.uint8))
    recognizer.predict(np.full((48, 320, 3), 255, dtype=np.uint8))
    MODEL = (detector, recognizer, SortQuadBoxes(), CropByPolys(det_box_type="quad"))


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 24:
        raise RuntimeError("Set a private OCR token of at least 24 characters")
    await run_in_threadpool(load_model)
    if ASR_ENABLED:
        try:
            await run_in_threadpool(load_asr)
        except Exception as exc:
            # ASR uses the app's CPU fallback; keep healthy OCR serving.
            print(f"gpu_asr_startup_failed error_type={type(exc).__name__}", flush=True)
    yield


app = FastAPI(lifespan=lifespan)


def authenticate(request):
    if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + TOKEN):
        raise HTTPException(401, "unauthorized")


@app.get("/health")
def health(request: Request):
    authenticate(request)
    return {"ready": MODEL is not None, "provider": "paddleocr-gpu", "protocol": PROTOCOL,
            "interval": OCR_SAMPLE_INTERVAL_SECONDS, "maxFrames": OCR_MAX_FRAMES,
            "recBatch": REC_BATCH, "detBatch": DET_BATCH,
            "asrReady": ASR_READY, "asrProvider": "faster-whisper-gpu" if ASR_READY else None}


@app.post("/asr")
async def asr(request: Request):
    started = time.perf_counter()
    authenticate(request)
    if not ASR_READY:
        raise HTTPException(503, "GPU ASR unavailable")
    from app.evidence import _asr_items_from_audio
    from app.remote_asr import PROTOCOL as ASR_PROTOCOL, settings
    import numpy as np
    try:
        size = int(request.headers["content-length"])
        samples = int(request.headers["x-asr-samples"])
        duration = float(request.headers["x-asr-duration"])
        if (not math.isfinite(duration) or not 0 < duration <= 3600
                or not 0 < size <= 3600 * 32000 or not 0 < samples <= 3600 * 16000
                or request.headers.get("content-encoding") != "gzip"
                or json.loads(request.headers["x-asr-settings"]) != settings()):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "invalid ASR size, duration or settings")
    try:
        await asyncio.wait_for(ASR_ADMISSION.acquire(), timeout=1)
    except asyncio.TimeoutError:
        raise HTTPException(503, "GPU ASR busy")
    try:
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > size:
                raise HTTPException(413, "audio too large")
        if len(payload) != size:
            raise HTTPException(400, "incomplete audio upload")
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            pcm = decoder.decompress(payload, samples * 2 + 1)
            if (len(pcm) != samples * 2 or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data):
                raise ValueError()
        except (ValueError, zlib.error):
            raise HTTPException(400, "incomplete compressed audio")
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        inference_started = time.perf_counter()
        items = await run_in_threadpool(_asr_items_from_audio, audio, "gpu-remote-pcm")
        print(f"gpu_asr samples={len(audio)} bytes={size} upload_ms={(inference_started-started)*1000:.1f} "
              f"inference_ms={(time.perf_counter()-inference_started)*1000:.1f}", flush=True)
        return {"protocol": ASR_PROTOCOL, "source": {
            "kind": "audio", "status": "available", "provider": "faster-whisper-gpu", "items": items,
            "settings": settings(), "sampling": {"samples": len(audio), "sampleRate": 16000, "complete": True}}}
    finally:
        ASR_ADMISSION.release()


def run_pages(paths, _tesseract, _temp_dir):
    import cv2
    started = time.perf_counter()
    # PaddleX's filename reader rejects PGM, although OpenCV decodes it losslessly.
    images = [cv2.imread(str(path), cv2.IMREAD_COLOR) for path in paths]
    if any(image is None for image in images):
        raise ValueError("GPU OCR could not decode every input page")
    # Detection batches require equal shapes. Group subtitle/full-screen pages,
    # then restore their exact input positions, including blank pages.
    groups = {}
    for index, image in enumerate(images):
        groups.setdefault(image.shape, []).append(index)
    output = [[] for _ in images]
    crops = []
    positions = []
    detection_seconds = 0.0
    recognition_seconds = 0.0
    with GPU_LOCK:
        detector, recognizer, sort_boxes, crop_boxes = MODEL
        for indices in groups.values():
            phase = time.perf_counter()
            pages = detector.predict(input=[images[index] for index in indices],
                                     batch_size=DET_BATCH, limit_side_len=64,
                                     limit_type="min", max_side_limit=4000,
                                     thresh=0.3, box_thresh=0.6, unclip_ratio=1.5)
            detection_seconds += time.perf_counter() - phase
            if len(pages) != len(indices):
                raise ValueError("GPU OCR lost input pages")
            for index, page in zip(indices, pages):
                boxes = sort_boxes(page["dt_polys"])
                if len(boxes):
                    page_crops = list(crop_boxes(images[index], boxes))
                    if len(page_crops) != len(boxes):
                        raise ValueError("GPU OCR lost detected text crops")
                    for crop in page_crops:
                        positions.append((index, len(output[index])))
                        output[index].append(None)
                        crops.append(crop)
        # Official OCR recognizes each page separately. Batch text across frames
        # by aspect ratio, then restore page/reading order without dropping blanks.
        ordered = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / crops[i].shape[0])
        if ordered:
            phase = time.perf_counter()
            recognized = recognizer.predict(input=[crops[i] for i in ordered], batch_size=REC_BATCH)
            recognition_seconds = time.perf_counter() - phase
            if len(recognized) != len(ordered):
                raise ValueError("GPU OCR lost recognition crops")
            for crop_index, result in zip(ordered, recognized):
                page_index, text_index = positions[crop_index]
                output[page_index][text_index] = str(result["rec_text"])
    results = []
    for texts in output:
        # Keep empty pages in their original slots. They break subtitle runs.
        if any(text is None for text in texts):
            raise ValueError("GPU OCR result missing recognized text")
        results.append((0, _bounded_text(" ".join(str(text) for text in texts), 1000), ""))
    print(f"gpu_ocr_pages pages={len(paths)} crops={len(crops)} det_ms={detection_seconds*1000:.1f} "
          f"rec_ms={recognition_seconds*1000:.1f} duration_ms={(time.perf_counter()-started)*1000:.1f}", flush=True)
    return results, {"processes": 0, "batches": 1, "fallback_batches": 0}


@app.post("/ocr")
async def ocr(request: Request):
    authenticate(request)
    try:
        duration = float(request.headers["x-ocr-duration"])
        size = int(request.headers["content-length"])
        if not 0 < duration <= 3600 or not 0 < size <= 500 * 1024 * 1024:
            raise ValueError()
        if (float(request.headers["x-ocr-interval"]) != OCR_SAMPLE_INTERVAL_SECONDS
                or int(request.headers["x-ocr-max-frames"]) != OCR_MAX_FRAMES):
            raise ValueError()
    except (KeyError, ValueError):
        raise HTTPException(400, "invalid duration, size or sampling configuration")
    try:
        await asyncio.wait_for(ADMISSION.acquire(), timeout=1)
    except asyncio.TimeoutError:
        raise HTTPException(503, "GPU OCR busy")
    try:
        with tempfile.TemporaryDirectory(prefix="jbb-gpu-video-") as directory:
            video = Path(directory) / "source.mp4"
            received = 0
            with video.open("wb") as file:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > size:
                        raise HTTPException(413, "video too large")
                    file.write(chunk)
            if received != size:
                raise HTTPException(400, "incomplete video upload")
            source = await run_in_threadpool(_ocr_evidence_local, video, duration,
                                            page_runner=run_pages, provider="paddleocr-gpu")
            if source.get("status") != "available" or not (source.get("sampling") or {}).get("complete"):
                raise HTTPException(503, "GPU OCR did not cover every sampled page")
            return {"protocol": PROTOCOL, "source": source}
    finally:
        ADMISSION.release()
