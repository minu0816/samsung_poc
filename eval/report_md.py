"""결정적 report.md 생성기 (LLM 불필요).

report.json(aggregate 산출) + 런별 auto_score.json을 읽어 사람이 읽을 배치 리포트를
기계적으로 작성한다. 100런 배치도 즉시 self-report. Claude 정성 narrative가 필요하면
--source llm 로 aggregate 후 이 위에 덧쓰면 된다.

Usage:
  python3 -m eval.report_md <batch_dir>
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def _read_json(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def _fmt_ms(ms, sec=None) -> str:
    """ms 우선, 없으면 sec로 폴백해 'N.Ns' 표기."""
    if ms is not None:
        return f'{ms / 1000:.1f}s'
    if sec is not None:
        return f'{float(sec):.1f}s'
    return '—'


def _video_rel(batch_dir: Path, run_id: str) -> str:
    """실제 자동화 영상(automation.mp4=가상 디스플레이) 우선, 없으면 메인(main.mp4)."""
    rd = batch_dir / 'runs' / str(run_id)
    if (rd / 'automation.mp4').exists():
        return f'runs/{run_id}/automation.mp4'
    if (rd / 'main.mp4').exists():
        return f'runs/{run_id}/main.mp4'
    return '—'


def _prompt_short(prompt: str, limit: int = 120) -> str:
    prompt = (prompt or '').replace('|', '\\|')
    if len(prompt) > limit:
        return prompt[:limit - 2] + '…'
    return prompt


def render(batch_dir: str | Path) -> Path:
    batch_dir = Path(batch_dir).resolve()
    report = _read_json(batch_dir / 'report.json') or {}
    batch = _read_json(batch_dir / 'batch.json') or {}
    rel = report.get('reliability', {})
    speed = report.get('speed', {})
    dev = batch.get('device', {})

    n = report.get('n_runs', 0)
    lines: list[str] = []
    a = lines.append

    a(f"# 반복 테스트 리포트 — {batch.get('scenario_id', '?')}")
    a('')
    a(f"- 배치: `{batch_dir.name}`")
    a(f"- 실행 수(N): **{n}**")
    a(f"- 시작: {batch.get('started_at', '?')} / 종료: {batch.get('finished_at', '?')}")
    a(f"- 채점 소스: `{report.get('score_source', 'auto')}` (객관 자동채점)")
    if dev:
        a(f"- 기기: {dev.get('serial', '?')} / Android {dev.get('android', '?')} "
          f"/ Kakao T {dev.get('kakao_taxi_version', '?')} / Gemini {dev.get('gemini_version', '?')}")
    a('')

    # ---- 요약 ----
    a('## 요약')
    a('')
    a(f"- **성공률(pass)**: {rel.get('pass_rate')}  ·  완료도달률(done): {rel.get('done_rate')}")
    oc = report.get('overall_counts', {})
    a(f"- 판정 분포: " + ', '.join(f'{k}={v}' for k, v in oc.items()) if oc else '- 판정 분포: —')
    nc = report.get('needed_confirmation_runs')
    if nc:
        a(f"- 확인 필요(카카오T 진행 질문 자동 동의): **{nc}건** / {n}런")
    a('')
    a('### 명령 입력 → 동작 완료 시간 (submit→done)')
    a('')
    a('| 지표 | 값 |')
    a('|---|---|')
    a(f"| 평균 | {_fmt_ms(speed.get('avg_done_ms'), speed.get('avg_done_sec'))} |")
    a(f"| 중앙값(p50) | {_fmt_ms(speed.get('p50_done_ms'), speed.get('p50_done_sec'))} |")
    a(f"| p95 | {_fmt_ms(speed.get('p95_done_ms'), speed.get('p95_done_sec'))} |")
    a(f"| 표준편차 | {_fmt_ms(speed.get('stddev_done_ms'), speed.get('stddev_done_sec'))} |")
    mm = speed.get('minmax_done_ms') or speed.get('minmax_done_sec')
    is_ms = speed.get('minmax_done_ms') is not None
    if mm:
        lo = _fmt_ms(mm[0]) if is_ms else _fmt_ms(None, mm[0])
        hi = _fmt_ms(mm[1]) if is_ms else _fmt_ms(None, mm[1])
        a(f"| 최소~최대 | {lo} ~ {hi} |")
    else:
        a("| 최소~최대 | — ~ — |")
    a('')
    if report.get('accuracy_avg') is not None or report.get('deferral_avg') is not None:
        a(f"- 정확도 평균: {report.get('accuracy_avg')}  ·  결정위임(deferral) 평균: {report.get('deferral_avg')}")
        a('')

    # ---- 실패 유형 분포 ----
    a('## 실패 유형 분포')
    a('')
    fmc = rel.get('failure_mode_counts') or report.get('failure_mode_counts', {})
    if fmc:
        a('| failure_mode | count |')
        a('|---|---|')
        for k, v in sorted(fmc.items(), key=lambda kv: -kv[1]):
            a(f'| {k} | {v} |')
    else:
        a('_데이터 없음_')
    a('')
    erc = rel.get('end_reason_counts') or {}
    if erc:
        a('end_reason: ' + ', '.join(f'`{k}`={v}' for k, v in sorted(erc.items(), key=lambda kv: -kv[1])))
        a('')

    # ---- 프롬프트별 반복 결과 ----
    # 같은 프롬프트를 N회 반복했을 때 차수별 성공/실패·완료시간·평균을 본다.
    # (반복 배치는 보통 프롬프트 1종이지만, 여러 종이어도 프롬프트별로 묶는다.)
    per_run = report.get('per_run', [])
    prompt_by_rid: dict[str, str] = {}
    for r in per_run:
        rid = str(r.get('run_id', '?'))
        meta = _read_json(batch_dir / 'runs' / rid / 'meta.json') or {}
        prompt_by_rid[rid] = meta.get('prompt') or ''

    groups: dict[str, list] = {}
    for r in per_run:
        groups.setdefault(prompt_by_rid.get(str(r.get('run_id', '?')), ''), []).append(r)

    a('## 프롬프트별 반복 결과')
    a('')
    for prompt, runs in groups.items():
        done_ms_list = [r.get('speed', {}).get('done_ms') for r in runs
                        if r.get('speed', {}).get('done_ms') is not None]
        done_sec_list = [r.get('speed', {}).get('done_sec') for r in runs
                         if r.get('speed', {}).get('done_sec') is not None]
        n_total = len(runs)
        n_pass = sum(1 for r in runs if r.get('overall') == 'pass')
        rate = f'{n_pass / n_total * 100:.0f}%' if n_total else '—'
        if done_ms_list:
            avg = _fmt_ms(sum(done_ms_list) / len(done_ms_list))
        elif done_sec_list:
            avg = _fmt_ms(None, sum(done_sec_list) / len(done_sec_list))
        else:
            avg = '—'

        a(f"### `{_prompt_short(prompt)}`")
        a('')
        a(f"- 실행 **{n_total}회** · 성공 {n_pass} / 실패 {n_total - n_pass} · 성공률 **{rate}**")
        a(f"- 평균 완료시간(성공 런): **{avg}**")
        a('')
        a('| 차수 | 결과 | t_kakao | t_done | failure_mode |')
        a('|---|---|---|---|---|')
        for i, r in enumerate(runs, 1):
            sp = r.get('speed', {})
            ok = r.get('overall') == 'pass'
            mark = '✅ 성공' if ok else f"❌ {r.get('overall') or 'fail'}"
            if r.get('needed_confirmation'):
                mark += ' 🗣️확인'
            a(f"| {i}차 (#{r.get('run_id', '?')}) | {mark} | "
              f"{_fmt_ms(sp.get('kakao_first_ms'), sp.get('kakao_first_sec'))} | "
              f"{_fmt_ms(sp.get('done_ms'), sp.get('done_sec'))} | "
              f"{r.get('failure_mode') or '—'} |")
        a('')

    # ---- 런별 상세 ----
    a('## 런별 상세')
    a('')
    a('| # | 프롬프트 | t_kakao | t_done | end_reason | failure_mode | overall | 영상 |')
    a('|---|---|---|---|---|---|---|---|')
    for r in per_run:
        rid = r.get('run_id', '?')
        sp = r.get('speed', {})
        prompt = _prompt_short(prompt_by_rid.get(str(rid), ''))
        overall = r.get('overall') or '—'
        if r.get('needed_confirmation'):
            overall += ' 🗣️확인'
        a(f"| {rid} | {prompt} | {_fmt_ms(sp.get('kakao_first_ms'), sp.get('kakao_first_sec'))} | "
          f"{_fmt_ms(sp.get('done_ms'), sp.get('done_sec'))} | {r.get('end_reason') or '—'} | "
          f"{r.get('failure_mode') or '—'} | {overall} | "
          f"{_video_rel(batch_dir, rid)} |")
    a('')

    # ---- 실패 런 빠른 참조 ----
    fails = [r for r in report.get('per_run', []) if r.get('overall') != 'pass']
    if fails:
        a('## 실패/부분 런 — 영상 확인용')
        a('')
        a('automation.mp4=실제 자동화(가상 디스플레이), main.mp4=메인(Gemini 챗)')
        a('')
        for r in fails:
            rid = r.get('run_id', '?')
            rd = batch_dir / 'runs' / str(rid)
            vids = []
            if (rd / 'automation.mp4').exists():
                vids.append(f'`runs/{rid}/automation.mp4`')
            if (rd / 'main.mp4').exists():
                vids.append(f'`runs/{rid}/main.mp4`')
            vid_str = ' , '.join(vids) if vids else '—'
            a(f"- **{rid}** ({r.get('failure_mode')}): {vid_str}  — {r.get('notes') or ''}")
        a('')

    out = batch_dir / 'report.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('batch_dir')
    args = ap.parse_args()
    out = render(args.batch_dir)
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
