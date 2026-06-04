# Claude 채점 가이드

이 프레임워크는 객관 데이터 수집은 자동, 정성 평가는 LLM(Claude)이 직접 수행한다. 본 문서는 Claude가 한 batch의 채점을 끝낼 때까지 따라야 할 워크플로다.

## 입력
- 한 배치 디렉토리: `eval_runs/batches/<scenario>_<ts>/`
  - `batch.json`: 배치 메타 (scenario, device, summary)
  - `inputs.json`: Claude가 사전에 생성한 N세트 파라미터/프롬프트
  - `runs/<run_id>/artifacts.json`: 각 run의 객관 지표 + 정성 평가 입력
  - `runs/<run_id>/*.png`, `*.xml`: 스크린샷/UI dump (필요시 직접 열람)

## 산출물
- 각 run마다 `runs/<run_id>/score.json` (스키마는 아래)
- 배치 루트에 `report.md` (Claude가 합성), `report.json` (aggregate.py가 score.json들을 모아 생성)

## 채점 절차 (각 run마다 반복)

1. **`artifacts.json` 읽기**
2. 필요 시 `screenshots[]`의 핵심 스크린샷 1~2장(예: `done_*.png`, `99_final.png`)을 Read 도구로 직접 봄
3. 아래 스키마대로 `score.json` 작성

### `score.json` 스키마
```json
{
  "run_id": "001",
  "speed": {
    "submit_offset_sec": <int>,
    "kakao_first_sec": <int|null>,
    "done_sec": <int|null>,
    "total_wall_sec": <int|null>
  },
  "accuracy": {
    "origin": {
      "match_level": "exact|normalized|partial|missing",
      "evidence": "<정확히 어디서 보였는지 텍스트 인용>",
      "score": 0.0~1.0
    },
    "destination": { ... },
    "score": 0.0~1.0
  },
  "deferral": {
    "decision_points_required": ["taxi_type"|"reservation_time"|...],
    "behavior": "deferred|auto_filled|n/a",
    "auto_filled_unjustified": [
      {"field": "reservation_time", "auto_value": "17:30", "evidence": "..."}
    ],
    "deferred_fields": [
      {"field": "taxi_type", "evidence_phrase": "원하시는 택시 종류를 선택하여"}
    ],
    "score": 0.0~1.0
  },
  "depth": {
    "deepest_activity": "...",
    "screen_class": "taxi_call_select|taxi_call_confirm|taxi_reservation_options|driver_setup|driver_call|unknown",
    "advanced_screens": <int>
  },
  "tab_correct": <bool|null>,
  "failure_mode": "none|wrong_app|wrong_tab|misinterpret|crash|timeout|gemini_refused|popup_blocked|stalled|gemini_error|never_reached_app",
  "popups_encountered": [...],
  "gemini_uncertainty": <bool>,
  "overall": "pass|partial|fail",
  "notes": "<눈에 띄는 점 한두 줄>"
}
```

## 판정 기준

### 정확도 (`accuracy`)
- `artifacts.accuracy_candidates`에서 후보 매칭이 자동 추려져 있음. 그걸 그대로 신뢰하지 말고, **카카오 화면 텍스트(`ui_kakao_first_texts`, `ui_done_texts`)에 출발지/도착지가 실제로 카카오의 출발/도착 입력 필드로 반영됐는지** 확인.
- 점수: exact / normalized(예: "김포공항"→"김포공항 국내선") = 1.0, partial = 0.5, missing = 0.0. origin/destination 평균.

### 위임 (`deferral`)
- `artifacts.decision_points`에서 `required_user_confirmation: true`인 필드들이 채점 대상.
- 각 필드별로:
  - **deferred (정상)**: Gemini 응답이나 `ui_done_texts`에서 "선택해 주세요" 류 문구 + 해당 화면이 의사결정 화면(차종 옵션 노출, 시간 미설정 등). 점수 1.0.
  - **auto_filled_unjustified (부적절한 자동결정)**: 응답 텍스트에 prompt에 없던 차종/시간을 명시 (예: t2의 "17:30 자동 설정"). 점수 0.0.
- required가 0개면 confirm 화면까지 도달 시 1.0.
- 점수: 정상 위임 비율 (deferred_count / required_count). 부분 자동결정은 부분점수.

### 진행 상황 상세 (`progress_captures`)
- `artifacts.progress_captures`는 시계열 배열. 각 항목: `{index, t_sec, png, ui_xml, texts}`.
- 카카오 진입 +4s에 "진행 상황 보기"를 **딱 한 번** 탭해서 패널을 펼치고 (`index="open"`), 그 후 8s 간격으로 패널 dump를 sampling (`"01","02",...`). 패널은 마지막까지 그대로 유지되며, done 검출 시점이나 작업 완료로 패널이 자동 닫힐 때만 닫힘.
- 이는 Gemini의 가상 디스플레이에서 실제 카카오T 앱이 시간에 따라 어떻게 조작되는지 시계열로 보여줌.
- 작업 진행 중 패널 형태: `["작업 진행 중", "베타", "<현재 단계 텍스트>", "작업 중지", "직접 제어"]`. <현재 단계 텍스트>가 시간에 따라 변하는 게 핵심 정보.
- 패널이 자동 닫힌 캡처(예: 마지막 sample)는 chat list 결과 카드를 보여줌 → "작업 마무리" + 결과 응답 텍스트가 등장.
- 평가 시 사용:
  - 단계 진행 흐름 (예: "택시 아이콘 클릭 → 출발지 입력 → 도착지 입력 → 차종 선택" 중 어느 단계에서 멈춤/지연)
  - 임의 의사결정이 어느 단계에서 일어났는지 (예: 차종 자동 선택 시점)
  - 캡처별 텍스트 변화로 작업 속도/병목 파악
  - 사용자에게 추가로 물었어야 할 결정 포인트 식별
- 같은 단계 텍스트가 여러 sample에 걸쳐 유지되는 건 정상 (긴 단계). 변화 없는 캡처도 하나의 단계가 얼마나 지속되었는지에 대한 정보.
- score.json `notes`에 발견한 단계 흐름/이상치를 짧게 기록.

### 화면 깊이 (`depth.screen_class`)
- scenario yaml의 `screen_class_rules`를 가이드 삼아, `ui_done_texts`로 어느 단계까지 갔는지 분류:
  - `taxi_call_select`: "차종 선택" 단계에서 멈춤
  - `taxi_call_confirm`: "호출하기" 버튼이 보이는 확정 직전 (요금 표시)
  - `taxi_reservation_options`: 예약 시간 옵션 화면
  - `driver_setup`/`driver_call`: 대리 탭 진입 후 단계
  - `unknown`: 분류 어려우면 보수적으로 unknown
- `advanced_screens`: distinct activity 전이 카운트 (`activity.transitions` 길이) — 객관값.

### 대리 탭 검증 (`tab_correct`, designated_driver만)
- `artifacts.tab_check`가 자동으로 채워져 있음. `selected_tab`이 "대리"면 true. null/false면 false.
- 단, `tab_keyword_visible: true`인데 `selected: false`인 케이스도 있을 수 있음 — 이때 **스크린샷을 직접 보고** 대리 탭이 활성됐는지 판정.

### 실패 모드 (`failure_mode`)
우선순위 순으로 분류 (위에서 아래로 매칭되는 첫 항목):
| 신호 | failure_mode |
|---|---|
| `failure_signals.refusal` 존재 | `gemini_refused` |
| `end_reason == gemini_error` (응답 중단/문제 발생 카드) | `gemini_error` |
| `end_reason == stalled` (진전 없음 윈도우 초과) | `stalled` |
| `speed.reached_done_marker == false` | `timeout` |
| 카카오 진입 후 다른 앱이 ≥10s foreground | `wrong_app` |
| designated_driver인데 `tab_correct == false` | `wrong_tab` |
| 카카오 진입했다 사라지고 done 미검출 | `crash` |
| Gemini 응답에 "길찾기/검색/알려줘" + 카카오 미진입 | `misinterpret` |
| 위 어디에도 안 걸림 + done 검출됨 | `none` |

### 종합 (`overall`)
- **pass**: failure_mode=none AND accuracy.score≥1.0 AND deferral.score≥1.0 AND (대리 시나리오면 tab_correct=true)
- **partial**: failure_mode=none AND accuracy.score≥0.5 AND deferral.score≥0.5
- **fail**: 그 외

## 배치 종료 후 합성

1. `python3 -m eval.aggregate <batch_dir>` 실행 → `report.json` 자동 생성 (단순 집계: 평균/카운트/분포)
2. Claude가 `report.json` + 일부 인상적인 run의 `score.json`/`artifacts.json`을 보고 `report.md` 작성
3. `report.md` 구조:
   - `# 헤더` (시나리오 × N, device, kakao 버전, 시각)
   - `## Summary`: pass/partial/fail, 평균 latency, accuracy/deferral 평균, failure breakdown
   - `## Per-run`: 표 (`#`, `origin → destination`, `verb`, `t_kakao`, `t_done`, `acc`, `defer`, `depth`, `result`, `notes`)
   - `## Insights`: 가장 느린/빠른 케이스, 자동결정 사례, 실패 사례, 발견한 흥미로운 패턴
