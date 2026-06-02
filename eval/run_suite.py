"""테스트 케이스 스위트 러너.

단일 testcases.json(템플릿 + 파라미터)을 읽어 파라미터 조합(cartesian product)을
케이스로 펼치고, 각 케이스를 해당 시나리오 배치로 반복 실행한다. 케이스별 산출물은
기존과 동일(automation.mp4 / main.mp4 / report.md / runs/...). 그 위에 스위트 전체
롤업 suite_report.md 를 추가 생성한다.

testcases.json 형식:
{
  "suite": "taxi_matrix",
  "repeat": 1,
  "max_obs_sec": 600,
  "groups": [
    { "scenario": "taxi_reserve",
      "template": "{origin}에서 {destination} 가는 {taxi_type} 택시 {reservation_time}로 예약해줘",
      "phrasing_verb": "예약해줘",
      "params": {
        "origin": ["서울역"], "destination": ["강남역"],
        "taxi_type": ["벤티", "블랙"],          # 여러 값 → 각각 다른 케이스
        "reservation_time": ["내일 오후 7시 30분"]  # 10분 단위 권장
      } }
  ]
}
- params의 각 키는 template의 {placeholder} 이름과 일치. 값 리스트의 조합(cartesian)이
  모두 별도 케이스가 된다. (예: taxi_type 2개 × 나머지 1개 = 2 케이스)
- groups 없이 최상위에 scenario/template/params를 둔 축약형도 허용.

Usage:
  python3 -m eval.run_suite testcases.json [--repeat N] [--max-obs-sec S]
      [--keep-video all|fail|none] [--video-bitrate 4M] [--video-fps 15] [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / 'scenarios'
SUITES_DIR = REPO_ROOT / 'eval_runs' / 'suites'


def _sanitize(s: str) -> str:
    """디렉토리/식별자용으로 안전한 문자만 남김(한글 유지)."""
    return re.sub(r'[^0-9A-Za-z가-힣]+', '', str(s)) or 'x'


def expand_group(group: dict) -> list[dict]:
    """group을 케이스 리스트로 펼친다.
    combine="product"(기본): 파라미터 값들의 모든 조합(cartesian).
    combine="zip": 파라미터 값들을 인덱스로 짝지어(서로 다른 N개) 케이스 생성.
                   각 파라미터 길이는 N 또는 1(1이면 broadcast)이어야 함.
    """
    scenario = group['scenario']
    template = group['template']
    params = group.get('params', {})
    combine = group.get('combine', 'product')
    extra = {k: v for k, v in group.items()
             if k not in ('scenario', 'template', 'params', 'combine')}
    keys = list(params.keys())
    value_lists = [params[k] for k in keys]

    if combine == 'zip':
        n = max((len(v) for v in value_lists), default=1)
        norm = []
        for k, v in zip(keys, value_lists):
            if len(v) == n:
                norm.append(v)
            elif len(v) == 1:
                norm.append(v * n)
            else:
                raise SystemExit(f"zip 모드: 파라미터 '{k}' 길이({len(v)})가 {n} 또는 1이 아님")
        combos = [dict(zip(keys, vals)) for vals in zip(*norm)]
    else:
        combos = [dict(zip(keys, vals)) for vals in product(*value_lists)]

    varying = [k for k in keys if len({str(c[k]) for c in combos}) > 1]
    cases = []
    for i, combo in enumerate(combos, start=1):
        prompt = template.format(**combo)
        if combine == 'zip':
            org = _sanitize(combo.get('origin', ''))
            label = f'{i:02d}' + (f'_{org}' if org and org != 'x' else '')
        elif varying:
            label = '_'.join(_sanitize(combo[k]) for k in varying)
        else:
            label = _sanitize('_'.join(str(combo[k]) for k in keys))
        cases.append({
            'id': f'{scenario}_{label}',
            'scenario': scenario,
            'prompt': prompt,
            **combo,
            **extra,
        })
    return cases


def expand_suite(suite: dict) -> list[dict]:
    groups = suite.get('groups') or [suite]
    cases: list[dict] = []
    for g in groups:
        cases.extend(expand_group(g))
    # id 중복 방지
    counts: dict[str, int] = {}
    for c in cases:
        base = c['id']
        if base in counts:
            counts[base] += 1
            c['id'] = f'{base}_{counts[base]:02d}'
        else:
            counts[base] = 1
    return cases


def scenario_path(name: str) -> Path:
    p = SCENARIOS_DIR / (name if str(name).endswith('.yaml') else f'{name}.yaml')
    if not p.exists():
        raise SystemExit(f'시나리오 파일 없음: {p}')
    return p


def run_case(case: dict, case_dir: Path, repeat: int, max_obs: int,
             keep_video: str, video_bitrate: str, video_fps: int) -> int:
    case_dir.mkdir(parents=True, exist_ok=True)
    # inputs.json = repeat개의 동일 케이스 (id 001..00N). scenario 키는 입력에서 제외.
    fields = {k: v for k, v in case.items() if k != 'scenario'}
    inputs = []
    for r in range(1, repeat + 1):
        e = dict(fields)
        e['id'] = f'{r:03d}'
        inputs.append(e)
    (case_dir / 'inputs.json').write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2), encoding='utf-8')

    cmd = [
        sys.executable, '-m', 'eval.orchestrator',
        '--scenario', str(scenario_path(case['scenario'])),
        '--batch-dir', str(case_dir),
        '--max-obs-sec', str(max_obs),
        '--keep-video', keep_video,
        '--video-bitrate', video_bitrate,
        '--video-fps', str(video_fps),
    ]
    print(f'\n===== CASE {case["id"]} ({case["scenario"]}) =====', flush=True)
    print(f'  prompt: {case["prompt"]}', flush=True)
    return subprocess.run(cmd, cwd=str(REPO_ROOT)).returncode


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _fmt_ms(ms) -> str:
    return f'{ms / 1000:.1f}s' if ms is not None else '—'


def write_suite_report(suite_dir: Path, suite_meta: dict, cases: list[dict]) -> Path:
    from eval.aggregate import _safe_avg, _safe_pct, _safe_stddev

    # 전 케이스의 런을 풀(pool)해서 스위트 전체 집계
    total_n = total_pass = total_done = total_confirm = 0
    all_done_ms: list[float] = []
    fmode_total: dict[str, int] = {}
    for c in cases:
        rep = _read_json(suite_dir / c['id'] / 'report.json')
        for r in rep.get('per_run', []):
            total_n += 1
            if r.get('overall') == 'pass':
                total_pass += 1
            if r.get('needed_confirmation'):
                total_confirm += 1
            sp = r.get('speed', {}) or {}
            if sp.get('reached_done_marker'):
                total_done += 1
            dm = sp.get('done_ms')
            if dm is None and sp.get('done_sec') is not None:
                dm = sp['done_sec'] * 1000
            if dm is not None:
                all_done_ms.append(dm)
            fm = r.get('failure_mode')
            if fm:
                fmode_total[fm] = fmode_total.get(fm, 0) + 1

    pass_rate = round(total_pass / total_n, 3) if total_n else None
    done_rate = round(total_done / total_n, 3) if total_n else None

    lines: list[str] = []
    a = lines.append
    a(f"# 스위트 리포트 — {suite_meta.get('suite', '?')}")
    a('')
    a(f"- 실행: {suite_meta.get('started_at', '?')} ~ {suite_meta.get('finished_at', '?')}")
    a(f"- 케이스 수: {len(cases)}  ·  케이스당 반복: {suite_meta.get('repeat')}회  "
      f"·  총 런: {total_n}  ·  타임아웃: {suite_meta.get('max_obs_sec')}s")
    a('')
    a('## 전체 요약')
    a('')
    a('| 지표 | 값 |')
    a('|---|---|')
    a(f"| 평균 성공률(pass) | {pass_rate if pass_rate is not None else '—'}"
      f"  ({total_pass}/{total_n}) |")
    a(f"| 완료율(done) | {done_rate if done_rate is not None else '—'}"
      f"  ({total_done}/{total_n}) |")
    a(f"| 평균 완료시간 | {_fmt_ms(_safe_avg(all_done_ms))} |")
    a(f"| 중앙값(p50) | {_fmt_ms(_safe_pct(all_done_ms, 50))} |")
    a(f"| p95 | {_fmt_ms(_safe_pct(all_done_ms, 95))} |")
    a(f"| 표준편차 | {_fmt_ms(_safe_stddev(all_done_ms))} |")
    a('')
    if fmode_total:
        a('실패유형 합계: ' + ', '.join(
            f'`{k}`×{v}' for k, v in sorted(fmode_total.items(), key=lambda kv: -kv[1])))
        a('')
    if total_confirm:
        a(f'확인 필요(카카오T 진행 질문 자동 동의) 합계: {total_confirm}건 / {total_n}런')
        a('')
    a('## 케이스별 요약')
    a('')
    a('| 케이스 | 시나리오 | 프롬프트 | n | 성공률 | 완료율 | 평균 done | p95 done | 실패유형 | 리포트 |')
    a('|---|---|---|---|---|---|---|---|---|---|')
    for c in cases:
        cid = c['id']
        rep = _read_json(suite_dir / cid / 'report.json')
        rel = rep.get('reliability', {})
        sp = rep.get('speed', {})
        n = rep.get('n_runs', 0)
        avg = sp.get('avg_done_ms')
        p95 = sp.get('p95_done_ms')
        avg_s = f'{avg/1000:.1f}s' if avg is not None else (
            f"{sp.get('avg_done_sec')}s" if sp.get('avg_done_sec') is not None else '—')
        p95_s = f'{p95/1000:.1f}s' if p95 is not None else (
            f"{sp.get('p95_done_sec')}s" if sp.get('p95_done_sec') is not None else '—')
        fmodes = rel.get('failure_mode_counts', {}) or rep.get('failure_mode_counts', {})
        fmode_s = ', '.join(f'{k}×{v}' for k, v in fmodes.items()) or '—'
        prompt = (c.get('prompt') or '').replace('|', '\\|')
        if len(prompt) > 120:
            prompt = prompt[:118] + '…'
        a(f"| {cid} | {c.get('scenario')} | {prompt} | {n} | "
          f"{rel.get('pass_rate', '—')} | {rel.get('done_rate', '—')} | {avg_s} | {p95_s} | "
          f"{fmode_s} | [report]({cid}/report.md) |")
    a('')
    a('각 케이스 폴더에 영상(automation.mp4=실제 자동화, main.mp4=메인)·런별 상세가 있습니다.')
    out = suite_dir / 'suite_report.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('testcases', help='testcases.json 경로')
    ap.add_argument('--repeat', type=int, default=None, help='케이스당 반복 횟수(파일값 override)')
    ap.add_argument('--max-obs-sec', type=int, default=None)
    ap.add_argument('--keep-video', choices=['all', 'fail', 'none'], default='all')
    ap.add_argument('--video-bitrate', default='4M')
    ap.add_argument('--video-fps', type=int, default=15)
    ap.add_argument('--suite-dir', default=None, help='출력 폴더 직접 지정(기본: eval_runs/suites/<suite>_<ts>)')
    ap.add_argument('--dry-run', action='store_true', help='케이스 펼치기만 출력(폰 미구동)')
    args = ap.parse_args()

    suite = _read_json(Path(args.testcases))
    if not suite:
        raise SystemExit(f'testcases 파일을 읽을 수 없음: {args.testcases}')
    cases = expand_suite(suite)
    repeat = args.repeat or int(suite.get('repeat', 1))
    max_obs = args.max_obs_sec or int(suite.get('max_obs_sec', 200))

    if args.dry_run:
        print(f'suite={suite.get("suite")} cases={len(cases)} repeat={repeat} max_obs={max_obs}')
        for c in cases:
            print(f'  - {c["id"]} [{c["scenario"]}] {c["prompt"]}')
        return

    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    suite_name = suite.get('suite', 'suite')
    suite_dir = Path(args.suite_dir) if args.suite_dir else SUITES_DIR / f'{suite_name}_{ts}'
    suite_dir.mkdir(parents=True, exist_ok=True)

    suite_meta = {
        'suite': suite_name,
        'started_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'repeat': repeat,
        'max_obs_sec': max_obs,
        'n_cases': len(cases),
        'cases': cases,
    }
    (suite_dir / 'suite.json').write_text(
        json.dumps(suite_meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[suite] {suite_name} → {suite_dir}  (cases={len(cases)}, repeat={repeat})', flush=True)

    for c in cases:
        run_case(c, suite_dir / c['id'], repeat, max_obs,
                 args.keep_video, args.video_bitrate, args.video_fps)

    suite_meta['finished_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
    (suite_dir / 'suite.json').write_text(
        json.dumps(suite_meta, ensure_ascii=False, indent=2), encoding='utf-8')
    out = write_suite_report(suite_dir, suite_meta, cases)
    print(f'\n[suite] 완료. 전체 롤업: {out}', flush=True)


if __name__ == '__main__':
    main()
