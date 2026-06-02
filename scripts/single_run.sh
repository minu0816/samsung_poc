#!/usr/bin/env bash
# Galaxy Gemini 단발성 실행 러너 — 시나리오 YAML 기반.
# Usage: single_run.sh <run_dir> <prompt> <scenario_yaml> [run_id]
set -uo pipefail

RUN_DIR="${1:?run_dir}"; PROMPT="${2:?prompt}"; SCENARIO="${3:?scenario_yaml}"; RUN_ID="${4:-$(basename "$RUN_DIR")}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/adb_helpers.sh
source "$SCRIPT_DIR/lib/adb_helpers.sh"
# shellcheck source=lib/ime_helpers.sh
source "$SCRIPT_DIR/lib/ime_helpers.sh"

mkdir -p "$RUN_DIR"

# YAML 필수 필드 파싱 (PyYAML). 한글 done_marker가 공백 포함이라 탭 구분자 사용.
SCENARIO_FIELDS=$(python3 - "$SCENARIO" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
done = cfg.get('done_marker', '작업 마무리')
mx = cfg.get('max_obs_sec', 200)
pkg = cfg.get('target_app', {}).get('package', 'com.kakao.taxi')
tab = cfg.get('target_app', {}).get('tab_keyword') or 'NULL'
print('\t'.join([str(done), str(mx), str(pkg), str(tab)]))
PY
)
IFS=$'\t' read -r DONE_MARKER MAX_OBS PKG TAB_KEYWORD <<< "$SCENARIO_FIELDS"

# orchestrator가 --max-obs-sec로 넘긴 값이 있으면 시나리오 기본을 override (10~600 clamp).
if [ -n "${MAX_OBS_OVERRIDE:-}" ]; then
  MAX_OBS="$MAX_OBS_OVERRIDE"
  [ "$MAX_OBS" -lt 10 ] && MAX_OBS=10
  [ "$MAX_OBS" -gt 600 ] && MAX_OBS=600
fi

# stall(진전 없음) 판정 윈도우(ms). orchestrator가 STALL_WINDOW_MS로 전달, 기본 45s.
STALL_WINDOW_MS="${STALL_WINDOW_MS:-45000}"
DONE_POLL_MS=1000   # 카카오 진입 후 done_marker 체크 간격(보고용)

START=$(date +%s)
START_MS=$(now_ms)
echo "[$RUN_ID] start prompt='$PROMPT' pkg=$PKG max_obs=${MAX_OBS}s"

# meta.json 텍스트 필드 기본값(SIGTERM 등 조기 종료 시 최소 meta가 유효하도록 선 초기화).
RESP='[]'; DONE_TEXTS='[]'; KAKAO_FIRST_TEXTS='[]'
PROGRESS_CAPTURES='[]'; PROGRESS_MID_TEXTS='[]'
SUBMIT=$START; SUBMIT_MS=$START_MS
KAKAO_FIRST=""; KAKAO_FIRST_MS=""; KAKAO_DEEPEST=""; KAKAO_LAST_SEEN=""
DONE_AT=""; DONE_MS=""; PROGRESS_OPEN_T=""; PROGRESS_SAMPLES=0
END_REASON=""; END_REASON_EVIDENCE=""
# 카카오T 진입 전 Gemini 진행-확인 질문 자동 동의 상태 (조기 종료 시에도 유효하도록 선초기화)
NEEDED_CONFIRMATION=0; CONFIRM_COUNT=0; CONFIRM_AT=""; CONFIRM_EVIDENCE=""
META_WRITTEN=0
# Gemini 가상 디스플레이(실제 자동화 화면) 녹화/캡처 상태
SCRCPY_VD_PID=""; VD_LOGICAL=""; VD_SF=""; VD_RECORDING=0; VD_DETECTED_T=""

# meta.json 작성 함수. 정상 종료/조기 종료(SIGTERM) 양쪽에서 호출 → 항상 유효한 기록을 남김.
# 텍스트 배열 변수(RESP 등)는 위에서 '[]'로 선초기화되어, 조기 종료 시에도 빈 배열로 안전.
write_meta () {
  local submit_offset_ms="null" kakao_first_ms="null" done_ms_field="null" k2d_ms="null"
  if [ -n "$START_MS" ] && [ -n "$SUBMIT_MS" ]; then submit_offset_ms=$((SUBMIT_MS - START_MS)); fi
  if [ -n "$KAKAO_FIRST_MS" ] && [ -n "$SUBMIT_MS" ]; then kakao_first_ms=$((KAKAO_FIRST_MS - SUBMIT_MS)); fi
  if [ -n "$DONE_MS" ] && [ -n "$SUBMIT_MS" ]; then done_ms_field=$((DONE_MS - SUBMIT_MS)); fi
  if [ -n "$DONE_MS" ] && [ -n "$KAKAO_FIRST_MS" ]; then k2d_ms=$((DONE_MS - KAKAO_FIRST_MS)); fi
  cat > "$RUN_DIR/meta.json" <<EOF
{
  "id": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$RUN_ID"),
  "scenario_yaml": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$SCENARIO"),
  "prompt": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1], ensure_ascii=False))" "$PROMPT"),
  "submit_offset_sec": $((SUBMIT - START)),
  "submit_offset_ms": ${submit_offset_ms},
  "kakao_first_sec_after_submit": ${KAKAO_FIRST:-null},
  "kakao_first_ms_after_submit": ${kakao_first_ms},
  "kakao_last_seen_sec_after_submit": ${KAKAO_LAST_SEEN:-null},
  "done_at_sec_after_submit": ${DONE_AT:-null},
  "done_ms_after_submit": ${done_ms_field},
  "kakao_first_to_done_ms": ${k2d_ms},
  "done_poll_ms": ${DONE_POLL_MS},
  "end_reason": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "${END_REASON:-unknown}"),
  "end_reason_evidence": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1], ensure_ascii=False))" "${END_REASON_EVIDENCE:-}"),
  "kakao_deepest_activity": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1], ensure_ascii=False))" "$KAKAO_DEEPEST"),
  "needed_confirmation": $([ "$NEEDED_CONFIRMATION" -eq 1 ] && echo true || echo false),
  "confirmation_count": ${CONFIRM_COUNT:-0},
  "confirmation_at_sec": ${CONFIRM_AT:-null},
  "confirmation_evidence": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1], ensure_ascii=False))" "${CONFIRM_EVIDENCE:-}"),
  "extracted_response_texts": $RESP,
  "ui_done_texts": $DONE_TEXTS,
  "ui_kakao_first_texts": $KAKAO_FIRST_TEXTS,
  "progress_captures": $PROGRESS_CAPTURES,
  "ui_progress_mid_texts": $PROGRESS_MID_TEXTS,
  "progress_panel_opened_at_sec": ${PROGRESS_OPEN_T:-null},
  "progress_samples_count": $PROGRESS_SAMPLES,
  "vdisplay_logical_id": $([ -n "$VD_LOGICAL" ] && echo "$VD_LOGICAL" || echo null),
  "vdisplay_sf_id": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]) if sys.argv[1] else 'null')" "$VD_SF"),
  "automation_recorded": $([ "$VD_RECORDING" -eq 1 ] && echo true || echo false),
  "vd_detected_at_sec": ${VD_DETECTED_T:-null},
  "max_obs_sec": $MAX_OBS,
  "target_package": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$PKG"),
  "tab_keyword": $(python3 -c "import json,sys; v=sys.argv[1]; print('null' if v=='NULL' else json.dumps(v, ensure_ascii=False))" "$TAB_KEYWORD")
}
EOF
  META_WRITTEN=1
}

# ---------- 종료 핸들러 ----------
# 어떤 경로로 죽든 녹화를 정상 finalize하고, orchestrator의 SIGTERM(타임아웃 graceful kill)
# 시에도 최소 meta.json을 남겨 autoscore가 분류할 수 있게 한다.
on_term () {
  [ -z "$END_REASON" ] && END_REASON="timeout"
  [ -z "$END_REASON_EVIDENCE" ] && END_REASON_EVIDENCE="terminated by signal"
  stop_recording
  if [ "$META_WRITTEN" -eq 0 ]; then write_meta; fi
  prune_video "$RUN_DIR" "$END_REASON"
  exit 143
}
trap 'stop_recording' EXIT       # 안전망 (정상 경로는 아래에서 먼저 stop)
trap 'on_term' TERM INT

# ---------- reset ----------
reset_apps
set_adbime

# ---------- 화면 녹화 시작 (전체 trajectory 캡처) ----------
start_recording "$RUN_DIR"

# ---------- launch Gemini ----------
adb shell am start -n com.google.android.apps.bard/.shellapp.BardEntryPointActivity >/dev/null 2>&1
# Galaxy는 Bard를 googlequicksearchbox 프로세스로 띄우는 경우가 있음
wait_gemini_activity () {
  local timeout="${1:-8}"
  local deadline=$(($(date +%s) + timeout))
  local f
  while [ "$(date +%s)" -lt "$deadline" ]; do
    f=$(focus)
    if echo "$f" | grep -qE 'com.google.android.apps.bard|com.google.android.googlequicksearchbox'; then
      return 0
    fi
    sleep 0.3
  done
  return 1
}
wait_gemini_activity 8 || echo "[$RUN_ID] WARN: gemini activity not detected"
sleep 1.0
ui "$RUN_DIR/ui_open.xml"

# Gemini Live promo 다이얼로그 '아니요' 닫기 (있을 때만)
DISMISS=$(tap_node_by_attr "$RUN_DIR/ui_open.xml" 'text="아니요"')
if [ -n "$DISMISS" ]; then
  echo "[$RUN_ID] dismiss promo $DISMISS"
  adb shell input tap $DISMISS >/dev/null
  sleep 1
fi

# ---------- input ----------
adb shell input tap 478 1958 >/dev/null
sleep 0.6
broadcast_text "$PROMPT"
# 입력 반영 대기 (EditText 활성/텍스트 보이는지)
wait_for_node_text "$PROMPT" "$RUN_DIR/ui_typed.xml" 4 || ui "$RUN_DIR/ui_typed.xml"

# 보내기 좌표 — UI에서 동적 추출, 못 찾으면 기본값
SEND=$(tap_node_by_attr "$RUN_DIR/ui_typed.xml" 'content-desc="보내기"')
[ -z "$SEND" ] && SEND="978 2048"
adb shell input tap $SEND >/dev/null
SUBMIT=$(date +%s)
SUBMIT_MS=$(now_ms)
sleep 0.5
shot "$RUN_DIR/02_submitted.png"
echo "[$RUN_ID] submitted (offset=$((SUBMIT-START))s) send=$SEND"

# ---------- observe ----------
TRACE="$RUN_DIR/trace.tsv"
echo -e "t_sec\tactivity" > "$TRACE"
LAST_SHOT=-100
LAST_ACT=""
# 진행 상황 패널: 카카오 진입 +PROGRESS_FIRST_DELAY초 후 딱 한 번 펼치고 끝까지 유지.
# 그 동안 PROGRESS_SAMPLE_INTERVAL초 간격으로 패널 dump + 스크린샷을 떠서 단계 변화를 시계열로 기록.
# done 검출은 panel dump (혹은 자동 닫힌 chat list)에서 done_marker grep.
PROGRESS_OPENED=0             # 0=아직 안 펼침, 1=펼친 상태 유지
PROGRESS_OPEN_SCHEDULED=999999 # KAKAO_FIRST 잡히면 KAKAO_FIRST + DELAY로 갱신
PROGRESS_FIRST_DELAY=4
PROGRESS_NEXT_SAMPLE=999999   # 다음 sampling 시점
PROGRESS_SAMPLE_INTERVAL=8    # sampling 간격(s)
PROGRESS_RETRY_DELAY=3        # 버튼 미발견 시 재시도 간격(s)
POPUP_LOG="$RUN_DIR/popups.log"
PROGRESS_LOG="$RUN_DIR/progress_caps.log"
: > "$POPUP_LOG"
: > "$PROGRESS_LOG"

POPUP_TOKENS=("아니요" "허용" "다음에" "괜찮아요" "지금 안 함" "확인")
# Gemini가 프롬프트 단계에서 거부할 때 흔히 나오는 토큰 (카카오 진입 전 chat list에서 검출).
REFUSAL_TOKENS=("할 수 없" "지원되지 않" "제공해드릴 수 없" "도와드릴 수 없" "도와드릴 수가 없")

# 진전 없음(stalled) / 거부(refusal) / 팝업 차단(popup_blocked) 검출용 상태.
# stall/popup 판정은 초(T) 단위로 — now_ms(호스트 python spawn)를 매 루프 호출하지 않기 위함.
# now_ms는 핵심 지표(submit/kakao_first/done) 시점에만 사용.
STALL_WINDOW_SEC=$(( STALL_WINDOW_MS / 1000 ))
[ "$STALL_WINDOW_SEC" -lt 5 ] && STALL_WINDOW_SEC=5
LAST_TRANSITION_T=0               # activity 전이 시각(s) — 전이마다 갱신
LAST_PROGRESS_CHANGE_T=0          # progress 패널 단계 텍스트가 바뀐 시각(s)
LAST_PROGRESS_HASH=""
POPUP_SEEN_T=""                   # 팝업이 foreground로 처음 잡힌 시각(s)
POPUP_BLOCK_SEC=8                 # 팝업이 이 시간 이상 고정되면 차단으로 판정
REFUSAL_NEXT_T=2                  # 카카오 진입 전 refusal 체크 다음 시점(s)
REFUSAL_CHECK_INTERVAL=2
CONFIRM_MAX=2                     # 카카오T 진행-확인 질문 자동 응답 최대 횟수(무한루프 방지)
CONFIRM_COOLDOWN=8                # 자동 응답 후 화면 갱신 대기(중복 응답 방지, s)
DONE_NEXT_T=999999                # done 체크 다음 시점(s) — 카카오 진입 시 활성화
VD_NEXT_CHECK_T=0                 # 가상 디스플레이 탐지 다음 시점(s)
VD_CHECK_INTERVAL=2

while :; do
  T=$(($(date +%s) - SUBMIT))
  [ "$T" -ge "$MAX_OBS" ] && break
  ACT=$(focus)
  echo -e "${T}\t${ACT}" >> "$TRACE"

  # Gemini 가상 디스플레이(실제 자동화 화면) 동적 탐지 → 잡히면 automation.mp4 녹화 시작.
  # 가상 디스플레이는 Gemini가 작업을 시작할 때 생성되므로 전송 후 주기적으로 확인.
  if [ "$VD_RECORDING" -eq 0 ] && [ "$T" -ge "$VD_NEXT_CHECK_T" ]; then
    VD_LOGICAL=$(gemini_vdisplay_logical_id)
    if [ -n "$VD_LOGICAL" ]; then
      VD_SF=$(gemini_vdisplay_sf_id)
      if start_recording_vd "$RUN_DIR" "$VD_LOGICAL"; then
        VD_RECORDING=1
        VD_DETECTED_T=$T
        echo "[$RUN_ID] virtual display detected at +${T}s (logical=$VD_LOGICAL sf=$VD_SF) → automation.mp4"
        shot_vd "$RUN_DIR/vd_first_${T}s.png" "$VD_SF"
        # 가상 디스플레이에서 자동화가 돌면 메인 mCurrentFocus가 계속 null이라
        # KAKAO_FIRST가 안 잡힌다. → 가상 디스플레이 활성화를 "작업 시작" 신호로 보고
        # done 폴링을 시작(잔여 카드 오검출 방지 위해 최소 20s 이후부터).
        if [ "$T" -lt 20 ]; then DONE_NEXT_T=20; else DONE_NEXT_T=$T; fi
      fi
    fi
    VD_NEXT_CHECK_T=$((T + VD_CHECK_INTERVAL))
  fi

  # 활동 전이 감지 시 전이 시각(ms) 갱신 (stall 판정 기준) + 전이 1장만 캡처.
  # 주기적(10s) 보조 샷은 영상이 대체하므로 제거(스크린샷 최소화).
  if [ "$ACT" != "$LAST_ACT" ]; then
    LAST_ACT="$ACT"
    LAST_TRANSITION_T=$T
    shot "$RUN_DIR/shot_${T}s.png"
  fi

  case "$ACT" in
    *"$PKG"*)
      POPUP_SEEN_T=""
      if [ -z "$KAKAO_FIRST" ]; then
        KAKAO_FIRST=$T
        KAKAO_FIRST_MS=$(now_ms)
        ui "$RUN_DIR/ui_kakao_first.xml"
        PROGRESS_OPEN_SCHEDULED=$((T + PROGRESS_FIRST_DELAY))
        DONE_NEXT_T=$T   # 카카오 진입 즉시 done 체크 활성화 (1s 간격)
      fi
      KAKAO_DEEPEST="$ACT"
      KAKAO_LAST_SEEN=$T
      ;;
    *com.google.android.apps.bard*|*com.google.android.googlequicksearchbox*|"")
      POPUP_SEEN_T=""
      # 카카오 진입 전 Gemini 화면 — 프롬프트 거부 여부를 주기적으로 확인 (조기 종료).
      if [ -z "$KAKAO_FIRST" ] && [ "$T" -ge "$REFUSAL_NEXT_T" ]; then
        ui /tmp/ui_ref_$$.xml
        for tok in "${REFUSAL_TOKENS[@]}"; do
          if grep -q "$tok" /tmp/ui_ref_$$.xml 2>/dev/null; then
            END_REASON="refusal"
            END_REASON_EVIDENCE="$tok"
            cp /tmp/ui_ref_$$.xml "$RUN_DIR/ui_done.xml"
            shot "$RUN_DIR/refusal_${T}s.png"
            echo "[$RUN_ID] refusal detected at +${T}s ('$tok')"
            break
          fi
        done
        # 카카오T 진행-확인 질문 자동 동의: 'refusal' 아니고, 같은 덤프에 '카카오'와
        # 진행/질문 토큰이 동시에 있을 때만 "네 진행해주세요"로 응답해 흐름을 진행시킨다.
        # (사용자 프롬프트엔 '카카오'가 없어 echo 오탐 없음, 평서문은 질문 토큰이 없어 제외)
        REPLIED_CONFIRM=0
        if [ "$END_REASON" != "refusal" ] && [ "$CONFIRM_COUNT" -lt "$CONFIRM_MAX" ] \
           && grep -q "카카오" /tmp/ui_ref_$$.xml 2>/dev/null \
           && grep -qE "할까요|하시겠|진행하|예약할까요|호출할까요|이용할까요|확인해 주세요" /tmp/ui_ref_$$.xml 2>/dev/null; then
          echo "[$RUN_ID] kakao confirmation prompt detected at +${T}s → 자동 동의 '네 진행해주세요'"
          cp /tmp/ui_ref_$$.xml "$RUN_DIR/ui_confirm_${CONFIRM_COUNT}.xml"
          adb shell input tap 478 1958 >/dev/null
          sleep 0.4
          broadcast_text "네 진행해주세요"
          sleep 0.4
          ui /tmp/ui_creply_$$.xml
          CSEND=$(tap_node_by_attr /tmp/ui_creply_$$.xml 'content-desc="보내기"')
          [ -z "$CSEND" ] && CSEND="978 2048"
          adb shell input tap $CSEND >/dev/null
          rm -f /tmp/ui_creply_$$.xml
          NEEDED_CONFIRMATION=1
          [ -z "$CONFIRM_AT" ] && CONFIRM_AT=$T
          CONFIRM_COUNT=$((CONFIRM_COUNT + 1))
          CONFIRM_EVIDENCE="kakao proceed-confirmation auto-agreed at +${T}s (reply '네 진행해주세요')"
          REPLIED_CONFIRM=1
        fi
        rm -f /tmp/ui_ref_$$.xml
        [ "$END_REASON" = "refusal" ] && break
        # 응답했으면 화면 갱신 대기(쿨다운), 아니면 기존 간격으로 재검사.
        if [ "$REPLIED_CONFIRM" -eq 1 ]; then
          REFUSAL_NEXT_T=$((T + CONFIRM_COOLDOWN))
        else
          REFUSAL_NEXT_T=$((T + REFUSAL_CHECK_INTERVAL))
        fi
      fi
      ;;
    *)
      # 비-Gemini 비-카카오 다이얼로그/앱 — 팝업 차단 판정.
      for tok in "${POPUP_TOKENS[@]}"; do
        if echo "$ACT" | grep -q "$tok" 2>/dev/null; then
          echo -e "${T}\t${ACT}" >> "$POPUP_LOG"
          [ -z "$POPUP_SEEN_T" ] && POPUP_SEEN_T=$T
          if [ $(( T - POPUP_SEEN_T )) -ge "$POPUP_BLOCK_SEC" ]; then
            END_REASON="popup_blocked"
            END_REASON_EVIDENCE="$ACT"
            echo "[$RUN_ID] popup_blocked at +${T}s ($ACT)"
          fi
          break
        fi
      done
      [ "$END_REASON" = "popup_blocked" ] && break
      ;;
  esac

  # 진행 상황 패널 한 번 펼치기 (카카오 진입 +PROGRESS_FIRST_DELAY초 시점)
  if [ "$PROGRESS_OPENED" -eq 0 ] && [ -n "$KAKAO_FIRST" ] && [ "$T" -ge "$PROGRESS_OPEN_SCHEDULED" ]; then
    ui /tmp/ui_for_progress_$$.xml
    PROGRESS_BTN=$(tap_node_by_attr /tmp/ui_for_progress_$$.xml 'text="진행 상황 보기"')
    if [ -n "$PROGRESS_BTN" ]; then
      echo "[$RUN_ID] progress panel open at +${T}s ($PROGRESS_BTN)"
      adb shell input tap $PROGRESS_BTN >/dev/null
      sleep 1.0
      ui "$RUN_DIR/ui_progress_open.xml"
      PROGRESS_OPENED=1
      PROGRESS_OPEN_T=$T
      PROGRESS_NEXT_SAMPLE=$((T + PROGRESS_SAMPLE_INTERVAL))
      echo -e "open\t${T}\t-\tui_progress_open.xml" >> "$PROGRESS_LOG"
    else
      # 버튼 아직 안 보임 — Gemini가 처리 시작 전. 짧게 retry.
      PROGRESS_OPEN_SCHEDULED=$((T + PROGRESS_RETRY_DELAY))
    fi
    rm -f /tmp/ui_for_progress_$$.xml
  fi

  # 패널 열린 동안 주기적 sampling (BACK 안 누름 — 패널은 그대로 떠 있음)
  if [ "$PROGRESS_OPENED" -eq 1 ] && [ "$T" -ge "$PROGRESS_NEXT_SAMPLE" ]; then
    PROGRESS_SAMPLES=$((PROGRESS_SAMPLES + 1))
    IDX=$(printf '%02d' "$PROGRESS_SAMPLES")
    # 패널 텍스트 시계열(채점/stall 판정용) XML 덤프만 유지 — PNG는 영상이 대체.
    ui "$RUN_DIR/ui_progress_${IDX}.xml"
    PROGRESS_NEXT_SAMPLE=$((T + PROGRESS_SAMPLE_INTERVAL))
    echo -e "${IDX}\t${T}\t-\tui_progress_${IDX}.xml" >> "$PROGRESS_LOG"
    # 패널 단계 텍스트(=dump) 변화 추적 → stall 판정.
    PHASH=$(md5 -q "$RUN_DIR/ui_progress_${IDX}.xml" 2>/dev/null || echo "")
    if [ -n "$PHASH" ] && [ "$PHASH" != "$LAST_PROGRESS_HASH" ]; then
      LAST_PROGRESS_HASH="$PHASH"
      LAST_PROGRESS_CHANGE_T=$T
    fi
  fi

  # stalled 판정: 카카오 진입 후, done 없이 activity 전이도 패널 단계 변화도
  # STALL_WINDOW_SEC 동안 모두 멈춰 있으면 "진전 없음".
  if [ -n "$KAKAO_FIRST" ] && [ -z "$DONE_AT" ]; then
    if [ $((T - LAST_TRANSITION_T)) -ge "$STALL_WINDOW_SEC" ] \
       && [ $((T - LAST_PROGRESS_CHANGE_T)) -ge "$STALL_WINDOW_SEC" ]; then
      END_REASON="stalled"
      END_REASON_EVIDENCE="no activity/progress change for ${STALL_WINDOW_SEC}s; last_act=$LAST_ACT"
      echo "[$RUN_ID] stalled at +${T}s"
      break
    fi
  fi

  # done 검출: 카카오 진입 후부터 1s 간격 dump (마커는 앱 진입 전엔 안 나오므로 블라인드 불필요).
  # 패널이 열려있는 동안엔 dump 결과가 대개 패널 내용 (chat list가 가려짐).
  # 패널 안 카카오 미니뷰의 텍스트도 dump에 포함되므로, "작업 마무리"가 chat list에서
  # 패널 위로 떠올랐거나 패널이 자동 닫힌 경우 grep으로 검출 가능.
  # done 폴링: 카카오가 메인 포커스에 떴거나(KAKAO_FIRST) 가상 디스플레이가 활성(VD_RECORDING)이면 수행.
  # (가상 디스플레이 플로우에선 mCurrentFocus가 null이라 KAKAO_FIRST가 안 잡히므로 VD 신호로 보강)
  if { [ -n "$KAKAO_FIRST" ] || [ "$VD_RECORDING" -eq 1 ]; } && [ "$T" -ge "$DONE_NEXT_T" ]; then
    DONE_NEXT_T=$((T + 1))
    ui /tmp/ui_check_$$.xml
    if grep -q "$DONE_MARKER" /tmp/ui_check_$$.xml 2>/dev/null; then
      DONE_AT=$T
      DONE_MS=$(now_ms)
      END_REASON="done"
      # 이 dump에 "작업 진행 중" 헤더가 같이 있으면 패널이 아직 위에 떠있는 것 → BACK으로 닫음.
      # 없으면 패널이 이미 자동 닫혀 chat list가 보이는 상태 → BACK 누르면 한 단계 더 가버림.
      if [ "$PROGRESS_OPENED" -eq 1 ] && grep -q "작업 진행 중" /tmp/ui_check_$$.xml 2>/dev/null; then
        adb shell input keyevent KEYCODE_BACK >/dev/null 2>&1
        sleep 0.8
        ui "$RUN_DIR/ui_done.xml"
        PROGRESS_OPENED=0   # 패널 닫혔음을 기록
      else
        cp /tmp/ui_check_$$.xml "$RUN_DIR/ui_done.xml"
        # 패널이 자동 닫힘: 변수 정리 (cleanup 시 BACK 중복 방지)
        [ "$PROGRESS_OPENED" -eq 1 ] && PROGRESS_OPENED=0
      fi
      shot "$RUN_DIR/done_${T}s.png"
      [ "$VD_RECORDING" -eq 1 ] && shot_vd "$RUN_DIR/vd_done_${T}s.png" "$VD_SF"
      echo "[$RUN_ID] done_marker detected at +${T}s"
      break
    fi
  fi
  sleep 0.5
done

rm -f /tmp/ui_check_$$.xml /tmp/ui_ref_$$.xml

# 루프 종료 후, 패널이 아직 열려있으면 닫고 (timeout 케이스 등) 마지막 chat list 확인
if [ "$PROGRESS_OPENED" -eq 1 ] && [ -z "$DONE_AT" ]; then
  echo "[$RUN_ID] closing progress panel (no done detected)"
  adb shell input keyevent KEYCODE_BACK >/dev/null 2>&1
  sleep 0.8
  ui "$RUN_DIR/ui_done.xml"
  if grep -q "$DONE_MARKER" "$RUN_DIR/ui_done.xml" 2>/dev/null; then
    DONE_AT=$T
    DONE_MS=$(now_ms)
    END_REASON="done"
    echo "[$RUN_ID] done_marker found after panel close"
  fi
fi

# end_reason 최종 resolve (우선순위: done > refusal > popup_blocked > stalled > never_reached_app > timeout)
if [ -z "$END_REASON" ]; then
  if [ -n "$DONE_AT" ]; then
    END_REASON="done"
  elif [ -z "$KAKAO_FIRST" ] && [ "$VD_RECORDING" -eq 0 ]; then
    # 카카오도 못 뜨고 가상 디스플레이도 안 생김 → 앱 미진입
    END_REASON="never_reached_app"
    END_REASON_EVIDENCE="target package '$PKG' never reached foreground/virtual display within ${MAX_OBS}s"
  else
    # 카카오 진입 또는 가상 디스플레이 활성이었으나 done_marker 미검출
    END_REASON="timeout"
    END_REASON_EVIDENCE="automation ran (kakao_first=${KAKAO_FIRST:-NA}, vd=${VD_RECORDING}) but no done_marker within ${MAX_OBS}s"
  fi
fi
echo "[$RUN_ID] end_reason=$END_REASON"

shot "$RUN_DIR/99_final.png"
ui "$RUN_DIR/ui_final.xml"

# Gemini 응답 텍스트 추출 (도메인 키워드 포함된 노드만)
RESP=$(python3 - "$RUN_DIR/ui_final.xml" <<'PY'
import re, json, sys
try:
    xml = open(sys.argv[1], encoding='utf-8').read()
except Exception:
    print('[]'); sys.exit(0)
KEYWORDS = ['택시','호출','도착','출발','준비','완료','선택','벤티','블랙','모범','블루','일반',
            '예약','대리','요금','확인','버튼','정확','확정','시간','지원','수 없','죄송']
texts = []
seen = set()
for m in re.finditer(r'<node[^>]+text="([^"]+)"', xml):
    t = m.group(1)
    if any(k in t for k in KEYWORDS) and t not in seen:
        texts.append(t); seen.add(t)
print(json.dumps(texts, ensure_ascii=False))
PY
)

# ui_done.xml의 모든 텍스트 노드도 같이 패키징(평가 시 LLM이 봄)
DONE_TEXTS=$(python3 - "$RUN_DIR/ui_done.xml" <<'PY'
import re, json, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print('[]'); sys.exit(0)
xml = open(p, encoding='utf-8').read()
texts = []
seen = set()
for m in re.finditer(r'<node[^>]+text="([^"]+)"', xml):
    t = m.group(1).strip()
    if t and t not in seen:
        texts.append(t); seen.add(t)
print(json.dumps(texts, ensure_ascii=False))
PY
)

KAKAO_FIRST_TEXTS=$(python3 - "$RUN_DIR/ui_kakao_first.xml" <<'PY'
import re, json, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print('[]'); sys.exit(0)
xml = open(p, encoding='utf-8').read()
texts = []
seen = set()
for m in re.finditer(r'<node[^>]+text="([^"]+)"', xml):
    t = m.group(1).strip()
    if t and t not in seen:
        texts.append(t); seen.add(t)
print(json.dumps(texts, ensure_ascii=False))
PY
)

_extract_xml_texts () {
  python3 - "$1" <<'PY'
import re, json, sys, os
p = sys.argv[1]
if not os.path.exists(p):
    print('[]'); sys.exit(0)
xml = open(p, encoding='utf-8').read()
texts = []
seen = set()
for m in re.finditer(r'<node[^>]+text="([^"]+)"', xml):
    t = m.group(1).strip()
    if t and t not in seen:
        texts.append(t); seen.add(t)
print(json.dumps(texts, ensure_ascii=False))
PY
}

# 진행 패널 캡처 결과를 시계열 list로 정리.
# index="open"은 펼친 직후 첫 캡처, 이후 "01","02"...은 sampling.
PROGRESS_CAPTURES=$(python3 - "$RUN_DIR" "$PROGRESS_LOG" <<'PY'
import json, os, re, sys
run_dir, log = sys.argv[1], sys.argv[2]
out = []
if os.path.exists(log):
    for line in open(log, encoding='utf-8').read().splitlines():
        parts = line.split('\t')
        if len(parts) < 4: continue
        idx, t, png, ui_xml = parts[0], parts[1], parts[2], parts[3]
        ui_path = os.path.join(run_dir, ui_xml)
        texts = []
        if os.path.exists(ui_path):
            xml = open(ui_path, encoding='utf-8').read()
            seen = set()
            for m in re.finditer(r'<node[^>]+text="([^"]+)"', xml):
                v = m.group(1).strip()
                if v and v not in seen:
                    texts.append(v); seen.add(v)
        out.append({
            'index': idx,  # "open" or "01","02"...
            't_sec': int(t) if t.lstrip('-').isdigit() else None,
            'png': png,
            'ui_xml': ui_xml,
            'texts': texts,
        })
print(json.dumps(out, ensure_ascii=False))
PY
)

# 모든 캡처 텍스트의 합집합 (legacy 호환용)
PROGRESS_MID_TEXTS=$(python3 - "$PROGRESS_CAPTURES" <<'PY'
import json, sys
caps = json.loads(sys.argv[1])
seen = set(); out = []
for c in caps:
    for t in c.get('texts', []):
        if t not in seen:
            out.append(t); seen.add(t)
print(json.dumps(out, ensure_ascii=False))
PY
)

# meta.json (정상 종료 경로 — 위에서 추출한 텍스트 변수들을 그대로 사용)
write_meta

# 녹화 정지(mp4 finalize) + KEEP_VIDEO 정책에 따른 정리
stop_recording
prune_video "$RUN_DIR" "$END_REASON"

# cleanup (다음 run을 위해)
adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1
sleep 0.5

echo "[$RUN_ID] done. end_reason=$END_REASON submit=+0 kakao_first=+${KAKAO_FIRST:-NA}s done=+${DONE_AT:-NA}s done_ms=${DONE_MS:+$((DONE_MS-SUBMIT_MS))} deepest=$KAKAO_DEEPEST"
