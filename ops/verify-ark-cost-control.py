"""Bounded staging comparison. Reads retained tasks; never updates business rows."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

from app import processing, providers, storage
from app.recognition_cache import RecognitionCache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    connection = sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    rows = connection.execute("SELECT * FROM tasks WHERE result_json IS NOT NULL AND evidence_json IS NOT NULL "
                              "AND status IN ('done','review') ORDER BY created_at DESC").fetchall()
    history = dict(checked=0, oldReviewTriggers=0, newReviewTriggers=0, localReviewCandidates=0,
                   p0TriggersPreserved=True)
    old_tags = {'台词归属错', '漏台词', 'OS漏标', 'OS/VO混淆', '音频对白不一致', '音频对白需核对',
                '疑似漏台词片段', '正文混入校对备注', '事实臆造', '疑似漏画面文字片段', '字幕对白遗漏'}
    sample = None
    local_sample = None
    for row in rows:
        quality = json.loads(row['quality_json'] or '{}')
        script = json.loads(row['result_json'])
        old_trigger = (isinstance(quality.get('tailGapSec'), (int, float)) and quality['tailGapSec'] >= 8
                       or not processing.quality_gate(quality)[0]
                       or any(x.get('severity') == 'P0' or x.get('tag') in old_tags
                              for x in quality.get('issues', [])))
        new_trigger = processing._needs_automated_content_review(quality)
        history['checked'] += 1
        history['oldReviewTriggers'] += bool(old_trigger)
        history['newReviewTriggers'] += bool(new_trigger)
        if not processing.quality_gate(quality)[0]:
            assert new_trigger, 'P0 trigger lost'
        context = processing._automated_review_context(script, processing._automated_review_issues(quality), quality=quality)
        has_local_candidate = bool(new_trigger and providers._review_clip_bounds(context, row['duration_sec'] or 0))
        history['localReviewCandidates'] += has_local_candidate
        evidence = json.loads(row['evidence_json'])
        sources = {x.get('kind') for x in evidence.get('sources', []) if x.get('status') == 'available'}
        if (local_sample is None and has_local_candidate and 35 <= (row['duration_sec'] or 0) <= 75
                and {'audio', 'ocr'} <= sources):
            local_sample = row
        if sample is None and 35 <= (row['duration_sec'] or 0) <= 75 and {'audio', 'ocr'} <= sources:
            sample = row
    sample = local_sample or sample
    assert sample is not None, 'No short sample with retained audio/OCR evidence'
    connection.close()
    print('HISTORICAL', json.dumps(history), flush=True)
    path, temporary = storage.materialize_media(sample['stored_path'], 'ark-cost-validation')
    evidence = json.loads(sample['evidence_json'])
    duration = sample['duration_sec']
    evidence['durationSec'] = duration
    route = processing.select_ark_route(duration, evidence)
    records, scripts, qualities = {}, {}, {}
    cache = RecognitionCache('ark-cost-validation-'+args.commit[:12], sample['stored_path'], {'duration': duration})
    try:
        prepared = providers.ark_prepare_video(path, duration, source_uri=sample['stored_path'], cache=cache)
        for label, fps in [('baseline', 2.0), ('candidate', providers.ARK_VIDEO_FPS)]:
            # Files preprocessing fixes FPS at upload time, so never reuse a
            # differently sampled file. Private object URLs can reuse media.
            media = prepared
            if 'file_id' in prepared:
                media = providers.ark_prepare_video(path, duration, cache=cache.scoped(label),
                                                     force_files=True, sampling_fps=fps)
            tick = time.monotonic()
            with patch.object(providers, 'ARK_VIDEO_FPS', fps):
                script, usage = providers.ark_recognize(path, sample['title'], duration, evidence,
                                                       route['model'], prepared_video=media)
            quality = processing._evaluate_quality(script, 'ark', deepcopy(evidence), route)[0]
            scripts[label], qualities[label] = script, quality
            records[label] = dict(seconds=round(time.monotonic()-tick, 3), usage=usage,
                                  transcriptCoverage=quality.get('transcriptCoverage'),
                                  ocrCoverage=quality.get('ocrCoverage'),
                                  blocking=len(processing.quality_gate(quality)[1]),
                                  contentGaps=len(quality.get('contentGapRanges') or []))
            (output/(label+'.json')).write_text(json.dumps(dict(script=script, quality=quality), ensure_ascii=False))
            print(label.upper(), json.dumps(records[label]), flush=True)
        regression = processing._review_regression_reason(qualities['baseline'], qualities['candidate'])
        # Exercise actual encoded clip + timestamp validation on one measured
        # source issue. Keep its output private; this never edits the task.
        local = None
        for issue in processing._automated_review_issues(qualities['candidate']):
            context = processing._automated_review_context(scripts['candidate'], [issue])
            bounds = providers._review_clip_bounds(context, duration)
            if bounds:
                tick = time.monotonic()
                reviewed, usage = providers.ark_recognize(path, sample['title'], duration, evidence,
                    processing.ARK_TURBO_MODEL, review_context=context, prepared_video=prepared)
                local = dict(bounds=bounds, seconds=round(time.monotonic()-tick, 3), usage=usage,
                             sourceTimelineValidated=True)
                (output/'local-review.json').write_text(json.dumps(reviewed, ensure_ascii=False))
                print('LOCAL_REVIEW', json.dumps(local), flush=True)
                break
        assert local is not None, 'No measured local review exercised'
        report = dict(sourceCommit=args.commit, historical=history, sampleDurationSec=duration,
                      records=records, localReview=local, regression=regression,
                      passed=regression is None, businessDataUnchanged=True, sampleCount=1)
        (output/'report.json').write_text(json.dumps(report, indent=2))
        print('REPORT', json.dumps(report), flush=True)
        assert report['passed'], 'Staging comparison showed quality regression'
    finally:
        storage.cleanup_materialized(temporary)


if __name__ == '__main__':
    main()
