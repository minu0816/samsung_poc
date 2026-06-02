"""객관 자동채점 (LLM 불필요).

artifacts.json + meta.json만으로 각 run의 pass/fail과 실패유형을 결정적으로 산출해
auto_score.json을 만든다. 100런 배치를 사람/LLM 개입 없이 self-report 하게 하는 게 목적.

Claude의 정성 채점 score.json과는 **파일명을 분리**(auto_score.json)해 공존한다.
스키마는 score.json과 호환되도록 맞춰 aggregate.py가 --source auto 로 그대로 집계할 수 있다.

Usage:
  python3 -m eval.autoscore <run_dir>            # 단일 run
  python3 -m eval.autoscore <batch_dir> --batch  # 배치 전체 runs/*/
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import yaml


# end_reason → failure_mode 기본 매핑. (designated_driver tab 불일치는 아래에서 override)
_END_REASON_TO_MODE = {
    'done': 'none',
    'refusal': 'gemini_refused',
    'timeout': 'timeout',
    'stalled': 'stalled',
    'popup_blocked': 'popup_blocked',
    'never_reached_app': 'never_reached_app',
    'unknown': 'unknown',
}


def _read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def _load_scenario(artifacts: dict) -> dict:
    path = artifacts.get('scenario_yaml_path')
    if path and Path(path).exists():
        try:
            return yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
        except Exception:
            return {}
    return {}


def _best_level_score(matches: list[dict]) -> float:
    """candidate_matches 결과 리스트 → 0.0/0.5/1.0 점수."""
    best = 0.0
    for m in matches or []:
        lvl = m.get('level', '')
        if lvl in ('exact', 'exact_normalized'):
            return 1.0
        if lvl == 'substring' or lvl.startswith('jaccard'):
            best = max(best, 0.5)
    return best


def accuracy_ok(artifacts: dict) -> dict:
    """주문/이동 파라미터(origin·destination·store·menu)가 앱 화면 후보에
    등장했는지로 결정적 정확도 산출."""
    cand = artifacts.get('accuracy_candidates') or {}
    fields = {}
    for key in ('origin', 'destination', 'store', 'menu'):
        if artifacts.get('params', {}).get(key):
            fields[key] = _best_level_score(cand.get(key))
    if not fields:
        return {'score': None, 'fields': {}}
    score = round(sum(fields.values()) / len(fields), 2)
    return {'score': score, 'fields': fields}


def deferral_ok(artifacts: dict, scenario: dict) -> dict:
    """사용자가 프롬프트에 명시 안 한 결정항목(required_user_confirmation)에 대해
    Gemini가 defer_signal로 사용자에게 넘겼는지 확인. 객관 proxy."""
    decision_points = artifacts.get('decision_points') or []
    required = [dp for dp in decision_points if dp.get('required_user_confirmation')]
    if not required:
        return {'score': None, 'required_fields': [], 'deferred': True,
                'auto_filled_unjustified': False}

    defer_signals = scenario.get('defer_signals') or []
    text_pool = ' '.join(
        (artifacts.get('gemini_response_texts') or [])
        + (artifacts.get('ui_done_texts') or [])
        + (artifacts.get('ui_progress_mid_texts') or [])
    )
    deferred = any(sig in text_pool for sig in defer_signals)
    reached_done = bool(artifacts.get('speed', {}).get('reached_done_marker'))
    auto_filled = reached_done and not deferred
    return {
        'score': 1.0 if deferred else 0.0,
        'required_fields': [dp['field'] for dp in required],
        'deferred': deferred,
        'auto_filled_unjustified': auto_filled,
    }


def classify_failure_mode(meta: dict, artifacts: dict, scenario: dict, tab_correct) -> str:
    end_reason = meta.get('end_reason') or artifacts.get('end_reason')
    # end_reason이 없는 구버전/부분 데이터는 객관 신호로 추론.
    if not end_reason or end_reason == 'unknown':
        speed = artifacts.get('speed', {})
        if speed.get('reached_done_marker'):
            end_reason = 'done'
        elif speed.get('kakao_first_sec') is not None:
            end_reason = 'timeout'
        else:
            end_reason = 'never_reached_app'
    mode = _END_REASON_TO_MODE.get(end_reason, 'unknown')
    # designated_driver 등 tab_keyword 시나리오에서 done이어도 탭이 틀리면 wrong_tab.
    if mode == 'none' and tab_correct is False:
        return 'wrong_tab'
    return mode


def _tab_correct(artifacts: dict, scenario: dict) -> bool | None:
    tab_keyword = scenario.get('target_app', {}).get('tab_keyword')
    if not tab_keyword:
        return None
    tab = artifacts.get('tab_check') or {}
    sel = tab.get('selected_tab')
    if sel and tab_keyword in sel:
        return True
    return False


def compute_overall(artifacts: dict, failure_mode: str, acc: dict, defer: dict,
                    tab_correct) -> str:
    reached_done = bool(artifacts.get('speed', {}).get('reached_done_marker'))
    if failure_mode != 'none' or not reached_done:
        return 'fail'
    # 여기부터 done + 실패신호 없음.
    acc_score = acc.get('score')
    defer_ok = (defer.get('score') is None) or (defer.get('score', 0) >= 1.0)
    tab_ok = (tab_correct is None) or (tab_correct is True)
    if acc_score is not None and acc_score < 0.5:
        return 'fail'
    if (acc_score is None or acc_score >= 1.0) and defer_ok and tab_ok:
        return 'pass'
    # done이지만 정확도 부분 매칭 또는 deferral 미흡 → partial
    return 'partial'


def autoscore_run(run_dir: str | Path) -> dict:
    run_dir = Path(run_dir).resolve()
    meta = _read_json(run_dir / 'meta.json') or {}
    artifacts = _read_json(run_dir / 'artifacts.json') or {}
    scenario = _load_scenario(artifacts)

    speed = artifacts.get('speed', {})
    tab_correct = _tab_correct(artifacts, scenario)
    failure_mode = classify_failure_mode(meta, artifacts, scenario, tab_correct)
    acc = accuracy_ok(artifacts)
    defer = deferral_ok(artifacts, scenario)
    overall = compute_overall(artifacts, failure_mode, acc, defer, tab_correct)

    score = {
        'auto': True,
        'run_id': artifacts.get('run_id') or meta.get('id'),
        'end_reason': meta.get('end_reason') or artifacts.get('end_reason'),
        'failure_mode': failure_mode,
        'overall': overall,
        'speed': {
            'submit_offset_sec': speed.get('submit_offset_sec'),
            'kakao_first_sec': speed.get('kakao_first_sec'),
            'done_sec': speed.get('done_sec'),
            'total_wall_sec': speed.get('total_wall_sec'),
            'submit_offset_ms': speed.get('submit_offset_ms'),
            'kakao_first_ms': speed.get('kakao_first_ms'),
            'done_ms': speed.get('done_ms'),
            'reached_done_marker': speed.get('reached_done_marker'),
        },
        'accuracy': acc,
        'deferral': defer,
        'depth': {
            'deepest_activity': artifacts.get('activity', {}).get('deepest_activity'),
            'screen_class': artifacts.get('screen_class'),
        },
        'tab_correct': tab_correct,
        'popups_encountered': artifacts.get('popups') or [],
        'gemini_uncertainty': bool((artifacts.get('failure_signals') or {}).get('uncertainty')),
        # 카카오T 진입 전 진행-확인 질문에 하네스가 자동 동의했는지(판정 불변, 태그만 부가).
        'needed_confirmation': bool(meta.get('needed_confirmation')),
        'confirmation_count': meta.get('confirmation_count') or 0,
        'notes': artifacts.get('end_reason_evidence') or meta.get('end_reason_evidence') or '',
    }
    (run_dir / 'auto_score.json').write_text(
        json.dumps(score, ensure_ascii=False, indent=2), encoding='utf-8')
    return score


def autoscore_batch(batch_dir: str | Path) -> list[dict]:
    batch_dir = Path(batch_dir).resolve()
    runs = sorted((batch_dir / 'runs').glob('*/'))
    out = []
    for r in runs:
        if (r / 'meta.json').exists() or (r / 'artifacts.json').exists():
            out.append(autoscore_run(r))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', help='run_dir, 또는 --batch면 batch_dir')
    ap.add_argument('--batch', action='store_true')
    args = ap.parse_args()
    if args.batch:
        scores = autoscore_batch(args.path)
        print(f'autoscored {len(scores)} runs')
    else:
        s = autoscore_run(args.path)
        print(json.dumps({k: s[k] for k in ('run_id', 'overall', 'failure_mode', 'speed')},
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
