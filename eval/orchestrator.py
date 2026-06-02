"""배치 실행 드라이버.

inputs.json (Claude가 미리 생성한 N세트)을 순서대로 single_run.sh에 위임,
각 run의 artifacts.json을 만들고, batch.json을 갱신한다.

Usage:
  python3 -m eval.orchestrator \\
    --scenario scenarios/taxi_call.yaml \\
    --batch-dir eval_runs/batches/taxi_call_20260505-143200 \\
    [--inputs <path>]   # 기본: <batch-dir>/inputs.json
    [--limit N]         # 디버그용
    [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SINGLE_RUN = REPO_ROOT / 'scripts' / 'single_run.sh'
ADB_HELPERS = REPO_ROOT / 'scripts' / 'lib' / 'adb_helpers.sh'


def device_info() -> dict:
    """ADB로 기기 정보 수집. ADB 미연결이면 빈 dict."""
    if not shutil.which('adb'):
        return {'error': 'adb not in PATH'}
    try:
        # adb_helpers.sh의 device_info_json 함수 사용
        out = subprocess.run(
            ['bash', '-c', f'source "{ADB_HELPERS}" && device_info_json'],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return {'error': out.stderr.strip()}
        return json.loads(out.stdout.strip() or '{}')
    except Exception as e:
        return {'error': str(e)}


def load_scenario(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding='utf-8'))


def _stamp_timeout_meta(run_dir: Path, run_id: str, max_obs: int) -> None:
    """SIGKILL 경로 등으로 single_run이 meta.json을 못 남긴 경우, autoscore가 분류할 수
    있도록 end_reason=timeout 최소 meta를 stamp한다. 이미 있으면 end_reason만 보정."""
    meta_path = run_dir / 'meta.json'
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except Exception:
            meta = {}
        meta.setdefault('end_reason', 'timeout')
        meta.setdefault('end_reason_evidence', 'killed by orchestrator timeout')
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    meta = {
        'id': run_id,
        'end_reason': 'timeout',
        'end_reason_evidence': 'killed by orchestrator timeout (no meta written)',
        'max_obs_sec': max_obs,
        'done_at_sec_after_submit': None,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


def run_single(run_dir: Path, prompt: str, scenario_path: Path, run_id: str,
               timeout_sec: int, max_obs: int, child_env: dict) -> int:
    """single_run.sh를 자체 프로세스 그룹으로 띄우고, 타임아웃 시 그룹에 SIGTERM(graceful)
    → 15s 후에도 살아있으면 SIGKILL. graceful kill이면 single_run의 SIGTERM 핸들러가
    녹화 mp4를 finalize하고 end_reason=timeout meta를 남긴다."""
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        'bash', str(SINGLE_RUN),
        str(run_dir), prompt, str(scenario_path), run_id,
    ]
    print(f'[orchestrator] {run_id} → {run_dir}', flush=True)
    env = {**os.environ, **child_env}
    proc = subprocess.Popen(cmd, start_new_session=True, env=env)
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        print(f'[orchestrator] {run_id} TIMEOUT after {timeout_sec}s — SIGTERM', flush=True)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(f'[orchestrator] {run_id} still alive — SIGKILL', flush=True)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        _stamp_timeout_meta(run_dir, run_id, max_obs)
        return 124


def extract_run(run_dir: Path, params: dict, run_id: str) -> dict:
    from eval.extract import extract
    return extract(run_dir, params=params, run_id=run_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario', required=True)
    ap.add_argument('--batch-dir', required=True)
    ap.add_argument('--inputs', default=None)
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--inter-run-sleep', type=float, default=2.0)
    # 반복 테스트: 동일 프롬프트 N회
    ap.add_argument('--count', '--repeat', type=int, default=None, dest='count',
                    help='동일 프롬프트를 N회 반복 (≤100). --prompt와 함께 사용.')
    ap.add_argument('--prompt', default=None, help='--count로 반복할 단일 프롬프트')
    ap.add_argument('--params-json', default=None,
                    help='--count 반복 시 구조화 파라미터 (origin/destination 등) JSON')
    # 반복 테스트: inputs.json 세트 × M회 순환
    ap.add_argument('--repeat-inputs', type=int, default=None,
                    help='inputs.json 전체를 M바퀴 순환 (id에 회차 suffix)')
    # 타임아웃/녹화/채점
    ap.add_argument('--max-obs-sec', type=int, default=None,
                    help='시나리오 max_obs_sec override (10~600 clamp)')
    # 아래 4개는 None이면 시나리오 yaml(recording/stall_window_sec) → 하드코딩 기본값 순으로 해석
    ap.add_argument('--keep-video', choices=['all', 'fail', 'none'], default='all')
    ap.add_argument('--video-bitrate', default=None)
    ap.add_argument('--video-fps', type=int, default=None)
    ap.add_argument('--stall-window-sec', type=int, default=None)
    ap.add_argument('--source', choices=['auto', 'llm'], default='auto')
    ap.add_argument('--no-report', action='store_true')
    args = ap.parse_args()

    scenario_path = Path(args.scenario).resolve()
    scenario = load_scenario(scenario_path)
    batch_dir = Path(args.batch_dir).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = batch_dir / 'runs'
    runs_dir.mkdir(exist_ok=True)

    # ---- 입력 결정: --count(단일 프롬프트 N회) / inputs.json(±--repeat-inputs 순환) ----
    if args.count:
        n = max(1, min(100, args.count))
        base = {}
        if args.params_json:
            base = json.loads(args.params_json)
        prompt = args.prompt or base.get('prompt')
        if not prompt:
            print('ERROR: --count에는 --prompt(또는 --params-json의 prompt)가 필요합니다.',
                  file=sys.stderr)
            sys.exit(2)
        inputs = [{**base, 'prompt': prompt, 'id': f'{i:03d}'} for i in range(1, n + 1)]
        # 재현용으로 합성 inputs를 배치에 저장
        (batch_dir / 'inputs.json').write_text(
            json.dumps(inputs, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        inputs_path = Path(args.inputs) if args.inputs else batch_dir / 'inputs.json'
        if not inputs_path.exists():
            print(f'ERROR: inputs.json not found at {inputs_path}', file=sys.stderr)
            sys.exit(2)
        inputs = json.loads(inputs_path.read_text(encoding='utf-8'))
        if args.repeat_inputs and args.repeat_inputs > 1:
            cycled = []
            for cyc in range(1, args.repeat_inputs + 1):
                for it in inputs:
                    base_id = it.get('id') or f'{len(cycled)+1:03d}'
                    cycled.append({**it, 'id': f'{base_id}_r{cyc:02d}'})
            inputs = cycled
    if args.limit:
        inputs = inputs[: args.limit]

    # batch.json (시작 시점 스냅샷)
    batch_meta = {
        'scenario_id': scenario.get('id'),
        'scenario_path': str(scenario_path),
        'started_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'device': device_info() if not args.dry_run else {},
        'n_inputs': len(inputs),
    }
    (batch_dir / 'batch.json').write_text(
        json.dumps(batch_meta, ensure_ascii=False, indent=2), encoding='utf-8',
    )
    print(f'[orchestrator] batch_dir={batch_dir} n={len(inputs)}', flush=True)

    max_obs = args.max_obs_sec or int(scenario.get('max_obs_sec', 200))
    max_obs = max(10, min(600, max_obs))  # 최대 10분
    # 단발 timeout = max_obs + 리셋/입력/teardown/영상 finalize 여유 90s
    per_run_timeout = max_obs + 90

    # 녹화/stall 설정 해석: CLI > 시나리오 yaml > 하드코딩 기본값
    rec = scenario.get('recording') or {}
    video_bitrate = args.video_bitrate or rec.get('bitrate') or '4M'
    video_fps = args.video_fps or rec.get('max_fps') or 15
    stall_window_sec = (args.stall_window_sec
                        or scenario.get('stall_window_sec') or 45)

    # single_run.sh로 전달할 환경 (override + 녹화/stall 설정)
    child_env = {
        'MAX_OBS_OVERRIDE': str(max_obs),
        'KEEP_VIDEO': args.keep_video,
        'VIDEO_BITRATE': str(video_bitrate),
        'VIDEO_FPS': str(video_fps),
        'STALL_WINDOW_MS': str(int(stall_window_sec) * 1000),
    }
    batch_meta['settings'] = {
        'max_obs_sec': max_obs, 'per_run_timeout_sec': per_run_timeout,
        'keep_video': args.keep_video, 'video_bitrate': str(video_bitrate),
        'video_fps': video_fps, 'stall_window_sec': stall_window_sec,
        'score_source': args.source,
    }

    summary = []
    for i, params in enumerate(inputs, start=1):
        run_id = params.get('id') or f'{i:03d}'
        run_dir = runs_dir / run_id
        prompt = params.get('prompt') or ''
        if not prompt:
            print(f'[orchestrator] SKIP {run_id}: empty prompt', flush=True)
            continue
        if args.dry_run:
            print(f'[DRY] would run {run_id}: {prompt}')
            summary.append({'run_id': run_id, 'status': 'dry'})
            continue

        rc = run_single(run_dir, prompt, scenario_path, run_id, per_run_timeout,
                        max_obs, child_env)

        # extract artifacts + 객관 자동채점 (실패해도 가능한 만큼)
        try:
            art = extract_run(run_dir, params, run_id)
            from eval.autoscore import autoscore_run
            sc = autoscore_run(run_dir)
            speed = art.get('speed', {})
            summary.append({
                'run_id': run_id,
                'rc': rc,
                'end_reason': art.get('end_reason'),
                'overall': sc.get('overall'),
                'failure_mode': sc.get('failure_mode'),
                'speed': speed,
                'reached_done': speed.get('reached_done_marker'),
            })
            print(f'[orchestrator] {run_id} overall={sc.get("overall")} '
                  f'failure={sc.get("failure_mode")} done={speed.get("done_ms")}ms', flush=True)
        except Exception as e:
            print(f'[orchestrator] extract/autoscore failed for {run_id}: {e}', file=sys.stderr)
            summary.append({'run_id': run_id, 'rc': rc, 'extract_error': str(e)})

        time.sleep(args.inter_run_sleep)

    # 배치 종료 시 IME 복원 (scrcpy 사용자가 한글 입력 막히지 않도록)
    if not args.dry_run and shutil.which('adb'):
        subprocess.run(['adb', 'shell', 'ime', 'reset'], capture_output=True, timeout=10)

    batch_meta['finished_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    batch_meta['summary'] = summary
    (batch_dir / 'batch.json').write_text(
        json.dumps(batch_meta, ensure_ascii=False, indent=2), encoding='utf-8',
    )

    # ---- 집계 + 리포트 자동 생성 (LLM 불필요) ----
    if not args.dry_run and not args.no_report:
        try:
            from eval.aggregate import aggregate
            from eval.report_md import render
            rep = aggregate(batch_dir, source=args.source)
            render(batch_dir)
            rel = rep.get('reliability', {})
            print(f'[orchestrator] report: pass_rate={rel.get("pass_rate")} '
                  f'done_rate={rel.get("done_rate")} '
                  f'p95_done_ms={rep.get("speed", {}).get("p95_done_ms")}', flush=True)
            print(f'[orchestrator] → {batch_dir / "report.md"}', flush=True)
        except Exception as e:
            print(f'[orchestrator] aggregate/report failed: {e}', file=sys.stderr)

    print(f'[orchestrator] done. summary at {batch_dir / "batch.json"}', flush=True)


if __name__ == '__main__':
    main()
