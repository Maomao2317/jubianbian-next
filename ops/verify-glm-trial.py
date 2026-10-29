"""Paid smoke test using synthetic media or an explicitly supplied 12s clip.

Never writes the task database or persists screenplay contents.
"""
from pathlib import Path
import asyncio
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, '/app/backend')
from app import config, glm_provider as glm, processing, providers
from app.evidence import collect_evidence
from app.script import has_screenplay_body, script_to_markdown


def forbidden(*args, **kwargs):
    raise AssertionError('GLM trial attempted a different provider')


def main(sample=None):
    assert config.VIDEO_PROVIDER == 'glm'
    assert config.GLM_REASONING_EFFORT == 'max'
    assert config.GLM_MAX_OUTPUT_TOKENS == 131072
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='glm-smoke-') as directory:
        video = Path(directory) / 'trial.mp4'
        if sample is not None:
            shutil.copyfile(sample, video)
            duration = float(subprocess.check_output(['ffprobe', '-v', 'error',
                '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', str(video)]))
            assert abs(duration - 12) < .2, 'Expected a 12-second trial clip'
        else:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=640x360:rate=12:duration=12', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(video)], check=True)
        evidence = {**collect_evidence(video, '智谱接入测试第1集', 12), 'durationSec': 12}
        prepared = glm.glm_prepare_video(video, 12)
        route = {'provider': 'glm', 'model': config.GLM_MODEL, 'mode': 'glm',
                 'band': 'glm_max', 'score': 0, 'reasons': ['explicit_glm_only']}
        # Fail closed even if retained provider credentials are available.
        with (patch.object(processing, 'ark_recognize', forbidden),
              patch.object(processing, 'ark_recognize_range', forbidden),
              patch.object(providers, 'ark_prepare_video', forbidden),
              patch.object(processing, 'openai_script', forbidden),
              patch.object(processing, 'openai_transcribe', forbidden),
              patch.object(processing, 'mark_task_stage')):
            script, quality, usage, _ = asyncio.run(processing._run_recognizer_with_media(
                {'id': 'glm-trial-smoke', 'title': '智谱接入测试第1集'},
                video, 12, evidence, route, prepared, None))
            assert usage['provider'] == 'glm' and usage['model'] == config.GLM_MODEL
            assert has_screenplay_body(script)
            markdown = script_to_markdown({'title': '智谱接入测试第1集', 'result': script, 'evidence': evidence})
            assert markdown.strip()
            # Exercise actual repair clipping and patch protocol even when the
            # synthetic initial result does not need an automatic quality pass.
            repaired, repair_usage, offset, duration = glm.glm_recognize_range(
                video, '智谱接入测试第1集', 12, evidence, config.GLM_MODEL,
                {'startSec': 2, 'endSec': 8})
            assert has_screenplay_body(repaired) and offset == 0 and duration == 10
            context = processing._automated_review_context(script, [
                {'severity': 'P0', 'tag': '事实臆造', 'startSec': 2, 'endSec': 8}])
            reviewed, review_usage = glm.glm_recognize(video, '智谱接入测试第1集', 12,
                evidence, config.GLM_MODEL, review_context=context, prepared_video=prepared)
            assert has_screenplay_body(reviewed)
        merged = processing._merge_usage(usage, repair_usage, review_usage)
        assert all(call.get('provider') == 'glm' for call in merged['calls'])
        result = {'provider': 'glm', 'model': config.GLM_MODEL, 'reasoningEffort': 'max',
                  'testVideoSeconds': 12, 'testMediaType': 'existing_clip' if sample else 'synthetic',
                  'elapsedSeconds': round(time.monotonic() - started, 2),
                  'sceneCount': len(script['scenes']),
                  'blockCount': sum(len(scene['blocks']) for scene in script['scenes']),
                  'markdownBytes': len(markdown.encode('utf-8')), 'repairVerified': True,
                  'patchReviewVerified': True, 'otherProvidersForbidden': True,
                  'deliveryStatus': quality.get('deliveryStatus'), 'usage': merged}
        Path('/tmp/glm-trial-verification.json').write_text(json.dumps(result, indent=2))
        print('GLM_LIVE_VIDEO_VERIFIED', json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', type=Path)
    main(parser.parse_args().sample)
