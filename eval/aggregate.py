"""score.json들을 모아 report.json 산출 (단순 집계).

Claude가 SCORING.md 가이드대로 각 run의 score.json을 작성한 뒤, 이 스크립트로
객관적 평균/카운트/breakdown을 정리한다. 정성적 insight나 narrative는 Claude가
직접 report.md에 쓴다.

Usage:
  python3 -m eval.aggregate <batch_dir>
"""
from __future__ import annotations
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path


def _safe_avg(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def _safe_minmax(xs: list[float]) -> tuple | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return (min(xs), max(xs))


def _safe_pct(xs: list[float], p: float) -> float | None:
    """p 분위수(0~100). 선형보간."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return round(float(xs[0]), 1)
    k = (len(xs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def _safe_stddev(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.pstdev(xs), 1) if len(xs) >= 2 else (0.0 if xs else None)


def aggregate(batch_dir: str | Path, source: str = 'auto') -> dict:
    """source='auto'면 auto_score.json(객관 자동채점), 'llm'이면 score.json(Claude) 집계."""
    batch_dir = Path(batch_dir).resolve()
    fname = 'auto_score.json' if source == 'auto' else 'score.json'
    runs = sorted((batch_dir / 'runs').glob(f'*/{fname}'))
    scores = [json.loads(p.read_text(encoding='utf-8')) for p in runs]

    n = len(scores)
    overall_counts = Counter(s.get('overall') for s in scores)
    failure_counts = Counter(s.get('failure_mode') for s in scores)
    end_reason_counts = Counter(s.get('end_reason') for s in scores if s.get('end_reason'))

    t_kakao = [s.get('speed', {}).get('kakao_first_sec') for s in scores]
    t_done = [s.get('speed', {}).get('done_sec') for s in scores]
    t_total = [s.get('speed', {}).get('total_wall_sec') for s in scores]
    t_done_ms = [s.get('speed', {}).get('done_ms') for s in scores]

    acc_scores = [s.get('accuracy', {}).get('score') for s in scores]
    defer_scores = [s.get('deferral', {}).get('score') for s in scores]

    # phrasing 별 pass율
    phrasing_stats: dict[str, dict] = {}
    # batch의 inputs.json에서 phrasing_verb 매핑
    inputs_map = {}
    inputs_path = batch_dir / 'inputs.json'
    if inputs_path.exists():
        for it in json.loads(inputs_path.read_text(encoding='utf-8')):
            inputs_map[it.get('id')] = it.get('phrasing_verb')

    for s in scores:
        verb = inputs_map.get(s.get('run_id'))
        if not verb:
            continue
        bucket = phrasing_stats.setdefault(verb, {'n': 0, 'pass': 0})
        bucket['n'] += 1
        if s.get('overall') == 'pass':
            bucket['pass'] += 1

    screen_class_counts = Counter(
        (s.get('depth', {}).get('screen_class') or 'unknown') for s in scores
    )

    popup_runs = sum(1 for s in scores if s.get('popups_encountered'))
    uncertainty_runs = sum(1 for s in scores if s.get('gemini_uncertainty'))
    confirmation_runs = sum(1 for s in scores if s.get('needed_confirmation'))

    auto_filled_count = sum(
        1 for s in scores if s.get('deferral', {}).get('auto_filled_unjustified')
    )

    n_pass = overall_counts.get('pass', 0)
    n_done = sum(1 for s in scores if s.get('speed', {}).get('reached_done_marker'))

    report = {
        'batch_dir': str(batch_dir),
        'score_source': source,
        'n_runs': n,
        'reliability': {
            'n': n,
            'pass_rate': round(n_pass / n, 3) if n else None,
            'done_rate': round(n_done / n, 3) if n else None,
            'failure_mode_counts': dict(failure_counts),
            'end_reason_counts': dict(end_reason_counts),
        },
        'overall_counts': dict(overall_counts),
        'failure_mode_counts': dict(failure_counts),
        'speed': {
            'avg_kakao_first_sec': _safe_avg(t_kakao),
            'minmax_kakao_first_sec': _safe_minmax(t_kakao),
            'avg_done_sec': _safe_avg(t_done),
            'minmax_done_sec': _safe_minmax(t_done),
            'avg_total_wall_sec': _safe_avg(t_total),
            # 명령 입력→완료 시간(ms): 100런 신뢰성의 핵심 지표
            'avg_done_ms': _safe_avg(t_done_ms),
            'p50_done_ms': _safe_pct(t_done_ms, 50),
            'p95_done_ms': _safe_pct(t_done_ms, 95),
            'stddev_done_ms': _safe_stddev(t_done_ms),
            'minmax_done_ms': _safe_minmax(t_done_ms),
            # 초 단위 분위수 (구 데이터/ms 미보유 시 폴백)
            'p50_done_sec': _safe_pct(t_done, 50),
            'p95_done_sec': _safe_pct(t_done, 95),
            'stddev_done_sec': _safe_stddev(t_done),
        },
        'accuracy_avg': _safe_avg(acc_scores),
        'deferral_avg': _safe_avg(defer_scores),
        'phrasing_pass_rate': {
            v: {'pass': b['pass'], 'n': b['n'],
                'rate': round(b['pass'] / b['n'], 2) if b['n'] else None}
            for v, b in phrasing_stats.items()
        },
        'screen_class_counts': dict(screen_class_counts),
        'popup_encounter_runs': popup_runs,
        'uncertainty_runs': uncertainty_runs,
        'needed_confirmation_runs': confirmation_runs,
        'auto_filled_unjustified_runs': auto_filled_count,
        'per_run': [
            {
                'run_id': s.get('run_id'),
                'overall': s.get('overall'),
                'end_reason': s.get('end_reason'),
                'failure_mode': s.get('failure_mode'),
                'speed': s.get('speed'),
                'accuracy_score': s.get('accuracy', {}).get('score'),
                'deferral_score': s.get('deferral', {}).get('score'),
                'screen_class': s.get('depth', {}).get('screen_class'),
                'tab_correct': s.get('tab_correct'),
                'needed_confirmation': s.get('needed_confirmation'),
                'confirmation_count': s.get('confirmation_count'),
                'notes': s.get('notes'),
            }
            for s in scores
        ],
    }

    out = batch_dir / 'report.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('batch_dir')
    ap.add_argument('--source', choices=['auto', 'llm'], default='auto')
    args = ap.parse_args()
    rep = aggregate(args.batch_dir, source=args.source)
    print(f'wrote {Path(args.batch_dir) / "report.json"} (n={rep["n_runs"]}, source={args.source})')
    print(json.dumps({
        'reliability': rep['reliability'],
        'speed': rep['speed'],
        'accuracy_avg': rep['accuracy_avg'],
        'deferral_avg': rep['deferral_avg'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
