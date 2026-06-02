"""run_dir의 raw artifacts → LLM이 평가하기 좋은 artifacts.json 패키징.

객관 지표(speed/depth/popups/screenshots)는 자동 산출, 정성 평가에 필요한
입력(prompt, params, scenario yaml, gemini 응답, 카카오 화면 텍스트, 대리탭 검출
등)도 함께 정리해서 단일 JSON으로 묶는다. 정성 채점(score.json)은 Claude가 이
파일을 읽고 별도로 작성한다.

Usage:
  python3 -m eval.extract <run_dir> [--params-json <inputs.json> --run-id 001]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from eval.ui_parse import all_texts, all_content_descs, selected_tab_text, has_text
from eval.normalize import candidate_matches


POPUP_TOKENS = ['아니요', '허용', '다음에', '괜찮아요', '지금 안 함', '확인']
GEMINI_REFUSAL_TOKENS = ['할 수 없', '지원되지 않', '제공해드릴 수 없', '도와드릴 수 없']
DECISION_VERBS = ['길찾기', '검색', '알려줘', '뭐야']
HEDGE_TOKENS = ['확인이 필요', '정확하지 않을', '말씀해 주시면', '확인해 주세요']


def _read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding='utf-8'))


def _read_trace(p: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    if not p.exists():
        return rows
    for line in p.read_text(encoding='utf-8').splitlines()[1:]:
        if '\t' not in line:
            continue
        t, act = line.split('\t', 1)
        try:
            rows.append((int(t), act))
        except ValueError:
            pass
    return rows


def _activity_summary(trace: list[tuple[int, str]]) -> dict:
    """trace를 압축해서 활동 전이 시퀀스만 남김. (LLM 입력 토큰 절약용)"""
    transitions: list[dict] = []
    last = None
    for t, act in trace:
        key = act.split('/')[0] if act else ''
        if key != last:
            transitions.append({'t': t, 'activity': act})
            last = key
    distinct_pkgs = sorted({a['activity'].split('/')[0] for a in transitions if a['activity']})
    return {'transitions': transitions, 'distinct_packages': distinct_pkgs}


def _detect_popups(run_dir: Path, trace: list[tuple[int, str]]) -> list[dict]:
    """popup.log + activity trace에서 비-Gemini 비-카카오 dialog 감지."""
    popups: list[dict] = []
    log = run_dir / 'popups.log'
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            if '\t' in line:
                t, act = line.split('\t', 1)
                popups.append({'t': int(t) if t.isdigit() else None, 'activity': act})
    return popups


def _detect_failure_signals(meta: dict, ui_done_texts: list[str], gem_resp: list[str]) -> dict:
    """객관적으로 단정할 수 있는 실패 신호만 자동 표시. 최종 분류는 autoscore/LLM."""
    signals = {}
    # single_run.sh가 판정한 end_reason을 1차 신호로 반영 (가장 신뢰도 높음).
    end_reason = meta.get('end_reason')
    if end_reason and end_reason not in ('done', 'unknown'):
        signals['end_reason'] = {
            'reason': end_reason,
            'evidence': meta.get('end_reason_evidence') or '',
        }
    # gemini refusal (텍스트 토큰 — end_reason 보강)
    for t in gem_resp + ui_done_texts:
        for tok in GEMINI_REFUSAL_TOKENS:
            if tok in t:
                signals['refusal'] = {'matched': tok, 'evidence': t}
                break
        if 'refusal' in signals:
            break
    # timeout / 미완
    if meta.get('done_at_sec_after_submit') is None:
        signals['timeout_or_unfinished'] = True
    # uncertainty
    for t in gem_resp + ui_done_texts:
        for tok in HEDGE_TOKENS:
            if tok in t:
                signals.setdefault('uncertainty', []).append({'matched': tok, 'evidence': t})
    return signals


def _derive_screen_class(scenario: dict, text_pool: list[str]) -> str | None:
    """시나리오 screen_class_rules와 화면 텍스트로 도달 화면을 결정적으로 분류."""
    rules = scenario.get('screen_class_rules') or {}
    pooled = ' '.join(text_pool)
    for cls, cond in rules.items():
        require_any = cond.get('require_any') or []
        if any(tok in pooled for tok in require_any):
            return cls
    return None


def _detect_tab_selected(ui_kakao_first: Path, ui_done: Path, tab_keyword: str | None) -> dict | None:
    if not tab_keyword:
        return None
    for ui in (ui_done, ui_kakao_first):
        if not ui.exists():
            continue
        sel = selected_tab_text(ui)
        if sel and tab_keyword in sel:
            return {'selected_tab': sel, 'evidence_xml': ui.name}
    # selected가 없을 때도 텍스트 노출만이라도 기록
    for ui in (ui_done, ui_kakao_first):
        if ui.exists() and has_text(ui, tab_keyword):
            return {'selected_tab': None, 'tab_keyword_visible': True, 'evidence_xml': ui.name}
    return {'selected_tab': None, 'tab_keyword_visible': False}


def _check_decision_points(scenario: dict, prompt: str) -> list[dict]:
    """prompt를 시나리오의 decision_points와 매칭, prompt에 안 나오면 'required'로 표시."""
    out = []
    for dp in scenario.get('decision_points', []):
        field = dp['field']
        keywords = dp.get('prompt_keywords') or []
        regex = dp.get('prompt_keywords_regex')
        specified = False
        matched = None
        for k in keywords:
            if k in prompt:
                specified = True; matched = k; break
        if not specified and regex:
            m = re.search(regex, prompt)
            if m:
                specified = True; matched = m.group(0)
        out.append({
            'field': field,
            'specified_in_prompt': specified,
            'prompt_evidence': matched,
            'required_user_confirmation': not specified,
        })
    return out


def _candidate_screenshots(run_dir: Path) -> list[str]:
    """LLM이 봐야 할 핵심 스크린샷 경로만 골라서 반환."""
    candidates = ['00_open.png', '01_typed.png', '02_submitted.png']
    for p in sorted(run_dir.iterdir()):
        n = p.name
        # vd_* = 가상 디스플레이(실제 자동화 화면) 캡처 — 우선 노출
        if (n.startswith('vd_') or n.startswith('kakao_first_') or n.startswith('done_')
                or n.startswith('progress_') or n == '99_final.png'):
            candidates.append(n)
    return [str((run_dir / n).resolve()) for n in candidates if (run_dir / n).exists()]


def extract(run_dir: str | Path, *, params: dict | None = None, run_id: str | None = None) -> dict:
    run_dir = Path(run_dir).resolve()
    meta = _read_json(run_dir / 'meta.json') or {}
    scenario_path = meta.get('scenario_yaml')
    scenario = yaml.safe_load(Path(scenario_path).read_text(encoding='utf-8')) if scenario_path else {}

    trace = _read_trace(run_dir / 'trace.tsv')
    act_summary = _activity_summary(trace)
    popups = _detect_popups(run_dir, trace)

    ui_done = run_dir / 'ui_done.xml'
    ui_kakao_first = run_dir / 'ui_kakao_first.xml'
    ui_final = run_dir / 'ui_final.xml'

    ui_done_texts = meta.get('ui_done_texts') or (all_texts(ui_done) if ui_done.exists() else [])
    ui_kakao_first_texts = meta.get('ui_kakao_first_texts') or (
        all_texts(ui_kakao_first) if ui_kakao_first.exists() else []
    )

    # 진행 상황 패널 시계열 캡처: meta.progress_captures가 우선, 없으면 ui_progress_*.xml fallback 스캔.
    # 인덱스는 "open"(첫 펼침) 또는 "01"/"02"...(이후 sampling) 문자열.
    progress_captures = meta.get('progress_captures')
    if not progress_captures:
        progress_captures = []
        # open이 먼저, 이후 숫자 순으로 정렬
        for p in sorted(run_dir.glob('ui_progress_*.xml'),
                        key=lambda x: (0, '') if x.stem.endswith('_open') else (1, x.stem)):
            stem = p.stem
            idx_str = stem.split('_')[-1]
            progress_captures.append({
                'index': idx_str,
                't_sec': None,
                'ui_xml': p.name,
                'png': None,
                'texts': all_texts(p),
            })
    # 합집합(legacy 호환)
    seen = set()
    ui_progress_mid_texts = []
    for c in progress_captures:
        for t in c.get('texts') or []:
            if t and t not in seen:
                ui_progress_mid_texts.append(t); seen.add(t)
    ui_final_texts = all_texts(ui_final) if ui_final.exists() else []
    gem_resp = meta.get('extracted_response_texts') or []

    prompt = meta.get('prompt') or (params or {}).get('prompt') or ''
    origin = (params or {}).get('origin')
    destination = (params or {}).get('destination')
    current_location = (params or {}).get('current_location')
    phrasing_verb = (params or {}).get('phrasing_verb')
    # 배민(주문) 시나리오 파라미터. 택시 시나리오에는 없으므로 None으로 무시된다.
    store = (params or {}).get('store')
    menu = (params or {}).get('menu')

    # 후보 텍스트 풀(앱 화면 + 응답)에서 파라미터 매칭 후보 추리기 (LLM 보조)
    candidate_pool = list({*ui_done_texts, *ui_kakao_first_texts, *gem_resp})
    accuracy_candidates = {}
    if origin:
        accuracy_candidates['origin'] = candidate_matches(origin, candidate_pool)
    if destination:
        accuracy_candidates['destination'] = candidate_matches(destination, candidate_pool)
    if current_location:
        accuracy_candidates['current_location'] = candidate_matches(current_location, candidate_pool)
    if store:
        accuracy_candidates['store'] = candidate_matches(store, candidate_pool)
    if menu:
        accuracy_candidates['menu'] = candidate_matches(menu, candidate_pool)

    decision_points = _check_decision_points(scenario, prompt)

    tab_check = _detect_tab_selected(
        ui_kakao_first, ui_done,
        scenario.get('target_app', {}).get('tab_keyword'),
    )

    failure_signals = _detect_failure_signals(meta, ui_done_texts, gem_resp)

    # 속도 지표 (초 + 밀리초)
    submit_offset = meta.get('submit_offset_sec')
    kakao_first = meta.get('kakao_first_sec_after_submit')
    done_at = meta.get('done_at_sec_after_submit')
    total_wall = None
    if submit_offset is not None and done_at is not None:
        total_wall = int(submit_offset) + int(done_at)
    elif submit_offset is not None and kakao_first is not None:
        total_wall = int(submit_offset) + int(kakao_first)

    submit_offset_ms = meta.get('submit_offset_ms')
    kakao_first_ms = meta.get('kakao_first_ms_after_submit')
    done_ms = meta.get('done_ms_after_submit')
    total_wall_ms = None
    if submit_offset_ms is not None and done_ms is not None:
        total_wall_ms = int(submit_offset_ms) + int(done_ms)

    end_reason = meta.get('end_reason')
    screen_class = _derive_screen_class(
        scenario,
        list({*ui_done_texts, *ui_kakao_first_texts, *ui_progress_mid_texts}),
    )

    artifacts = {
        'run_id': run_id or meta.get('id'),
        'scenario_id': scenario.get('id'),
        'scenario_yaml_path': str(scenario_path) if scenario_path else None,
        'params': {
            'origin': origin,
            'destination': destination,
            'current_location': current_location,
            'phrasing_verb': phrasing_verb,
            'store': store,
            'menu': menu,
        },
        'prompt': prompt,
        'end_reason': end_reason,
        'end_reason_evidence': meta.get('end_reason_evidence'),
        'screen_class': screen_class,
        'speed': {
            'submit_offset_sec': submit_offset,
            'kakao_first_sec': kakao_first,
            'done_sec': done_at,
            'total_wall_sec': total_wall,
            'submit_offset_ms': submit_offset_ms,
            'kakao_first_ms': kakao_first_ms,
            'done_ms': done_ms,
            'total_wall_ms': total_wall_ms,
            'reached_done_marker': done_at is not None,
        },
        'activity': {
            'deepest_activity': meta.get('kakao_deepest_activity'),
            'transitions': act_summary['transitions'],
            'distinct_packages': act_summary['distinct_packages'],
            'kakao_first_to_done_sec': (
                int(done_at) - int(kakao_first) if done_at is not None and kakao_first is not None else None
            ),
        },
        'gemini_response_texts': gem_resp,
        'ui_kakao_first_texts': ui_kakao_first_texts,
        'ui_done_texts': ui_done_texts,
        'progress_captures': progress_captures,
        'progress_caps_count': len(progress_captures),
        'ui_progress_mid_texts': ui_progress_mid_texts,
        'ui_final_texts': ui_final_texts,
        'accuracy_candidates': accuracy_candidates,
        'decision_points': decision_points,
        'tab_check': tab_check,
        'popups': popups,
        'failure_signals': failure_signals,
        'screenshots': _candidate_screenshots(run_dir),
    }

    out_path = run_dir / 'artifacts.json'
    out_path.write_text(json.dumps(artifacts, ensure_ascii=False, indent=2), encoding='utf-8')
    return artifacts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_dir')
    ap.add_argument('--params-json', default=None, help='inputs.json (또는 단일 dict json)')
    ap.add_argument('--run-id', default=None)
    args = ap.parse_args()

    params = None
    if args.params_json:
        data = json.loads(Path(args.params_json).read_text(encoding='utf-8'))
        if isinstance(data, list) and args.run_id:
            for r in data:
                if r.get('id') == args.run_id:
                    params = r; break
        elif isinstance(data, dict):
            params = data

    art = extract(args.run_dir, params=params, run_id=args.run_id)
    print(json.dumps({
        'run_id': art['run_id'],
        'speed': art['speed'],
        'tab_check': art['tab_check'],
        'failure_signals': art['failure_signals'],
        'decision_points': art['decision_points'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
