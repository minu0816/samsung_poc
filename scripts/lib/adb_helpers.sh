#!/usr/bin/env bash
# ADB 공용 헬퍼. source로 불러서 사용.

shot () { adb exec-out screencap -p > "$1"; }

# 현재 epoch 밀리초. macOS BSD date는 %3N 미지원이라 python 사용.
# 무거우니 루프 매 회가 아니라 이벤트 시점에만 호출할 것.
now_ms () { python3 -c 'import time;print(int(time.time()*1000))'; }

# ---------- scrcpy 호스트 측 화면 녹화 ----------
# 기기 인코더 부하가 adb screenrecord보다 낮고(타이밍 왜곡 최소), 180초 제한이 없으며
# pull 단계가 없어 10분 런을 단일 mp4로 안전하게 남긴다.
# 환경변수: VIDEO_FPS(기본 15), VIDEO_BITRATE(기본 4M), KEEP_VIDEO(all|fail|none, 기본 all).

# start_recording <run_dir> — 백그라운드 scrcpy 기동. PID를 전역 SCRCPY_PID 및 <run_dir>/scrcpy.pid에 기록.
start_recording () {
  local run_dir="$1"
  if ! command -v scrcpy >/dev/null 2>&1; then
    echo "[rec] WARN: scrcpy not found — recording disabled" >&2
    SCRCPY_PID=""
    return 0
  fi
  scrcpy --no-window --no-playback --no-audio \
    --max-fps="${VIDEO_FPS:-15}" --video-bit-rate="${VIDEO_BITRATE:-4M}" \
    --record="$run_dir/main.mp4" --record-format=mp4 \
    >"$run_dir/scrcpy.log" 2>&1 &
  SCRCPY_PID=$!
  echo "$SCRCPY_PID" > "$run_dir/scrcpy.pid"
}

# Gemini 화면 자동화는 별도의 비공개 "가상 디스플레이"(owner=googlequicksearchbox, 코드명 Bonobo)
# 에서 돈다. 메인(display 0) 녹화는 Gemini 챗만 담기므로, 실제 조작은 가상 디스플레이를 직접
# 녹화/캡처해야 보인다. scrcpy는 논리 displayId, screencap은 SurfaceFlinger id를 쓴다.

# gemini_vdisplay_logical_id — scrcpy --display-id 용 논리 id (없으면 빈값).
gemini_vdisplay_logical_id () {
  adb shell dumpsys display 2>/dev/null | tr -d '\r' | grep -oE 'DisplayViewport\{[^}]*\}' \
    | grep 'type=VIRTUAL' | grep 'googlequicksearchbox' \
    | grep -oE 'displayId=[0-9]+' | grep -oE '[0-9]+' | tail -1
}

# gemini_vdisplay_sf_id — screencap -d 용 SurfaceFlinger id (없으면 빈값).
gemini_vdisplay_sf_id () {
  adb shell dumpsys SurfaceFlinger --display-id 2>/dev/null | tr -d '\r' \
    | grep 'Virtual display' | grep -oE 'Display [0-9]+' | grep -oE '[0-9]+' | tail -1
}

# start_recording_vd <run_dir> <logical_id> — 가상 디스플레이를 automation.mp4로 녹화.
start_recording_vd () {
  local run_dir="$1" did="$2"
  [ -z "$did" ] && { SCRCPY_VD_PID=""; return 1; }
  if ! command -v scrcpy >/dev/null 2>&1; then SCRCPY_VD_PID=""; return 1; fi
  scrcpy --display-id="$did" --no-window --no-playback --no-audio \
    --max-fps="${VIDEO_FPS:-15}" --video-bit-rate="${VIDEO_BITRATE:-4M}" \
    --record="$run_dir/automation.mp4" --record-format=mp4 \
    >"$run_dir/scrcpy_vd.log" 2>&1 &
  SCRCPY_VD_PID=$!
  echo "$SCRCPY_VD_PID" > "$run_dir/scrcpy_vd.pid"
  return 0
}

# _stop_scrcpy <pid> — SIGINT으로 mp4 moov atom flush, 5s 대기 후 미종료면 TERM.
_stop_scrcpy () {
  local pid="$1"
  [ -z "$pid" ] && return 0
  kill -INT "$pid" 2>/dev/null
  local i
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
}

# stop_recording — 메인(SCRCPY_PID)·가상(SCRCPY_VD_PID) 녹화 둘 다 정상 종료.
stop_recording () {
  _stop_scrcpy "${SCRCPY_VD_PID:-}"; SCRCPY_VD_PID=""
  _stop_scrcpy "${SCRCPY_PID:-}";    SCRCPY_PID=""
}

# shot_vd <file> <sf_id> — 가상 디스플레이 스크린샷(없으면 display 0 폴백).
shot_vd () {
  local file="$1" sf_id="$2"
  if [ -n "$sf_id" ]; then
    adb exec-out screencap -d "$sf_id" -p > "$file" 2>/dev/null
  else
    adb exec-out screencap -p > "$file"
  fi
}

# prune_video <run_dir> <end_reason> — KEEP_VIDEO 정책에 따라 main.mp4 + automation.mp4 정리.
#   all(기본)=항상 보관 / fail=done이면 삭제 / none=항상 삭제
prune_video () {
  local run_dir="$1" end_reason="${2:-}"
  case "${KEEP_VIDEO:-all}" in
    none) rm -f "$run_dir/main.mp4" "$run_dir/automation.mp4" ;;
    fail) [ "$end_reason" = "done" ] && rm -f "$run_dir/main.mp4" "$run_dir/automation.mp4" ;;
    *)    : ;;  # all
  esac
}

focus () {
  adb shell dumpsys window 2>/dev/null \
    | grep -m1 mCurrentFocus \
    | sed 's/.*u0 //' | sed 's/}$//' | tr -d '\r'
}

ui () {
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1 \
    && adb pull /sdcard/ui.xml "$1" >/dev/null 2>&1
}

# wait_activity <substring> <timeout_sec> [poll_ms]
# foreground activity 문자열에 substring이 등장할 때까지 대기. 타임아웃이면 1.
wait_activity () {
  local substr="$1" timeout="${2:-8}" poll_ms="${3:-300}"
  local poll_s
  poll_s=$(awk "BEGIN{print $poll_ms/1000}")
  local deadline=$(($(date +%s) + timeout))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local f
    f=$(focus)
    if echo "$f" | grep -q "$substr"; then return 0; fi
    sleep "$poll_s"
  done
  return 1
}

# wait_for_node_text <text> <ui_xml_dest> <timeout_sec>
# uiautomator dump에서 text= 또는 content-desc=에 매칭되는 노드가 보일 때까지 대기.
wait_for_node_text () {
  local needle="$1" dest="$2" timeout="${3:-5}"
  local deadline=$(($(date +%s) + timeout))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    ui "$dest"
    if grep -q "$needle" "$dest" 2>/dev/null; then return 0; fi
    sleep 0.4
  done
  return 1
}

# 화면이 꺼져 있으면 깨우고, (비보안) 잠금화면이면 해제. 100런 무인 실행 중
# 런 사이 자동 잠금으로 Gemini가 안 뜨는 것을 방지. 보안 잠금(PIN)일 땐 best-effort.
wake_device () {
  if ! adb shell dumpsys power 2>/dev/null | grep -m1 mWakefulness | grep -q Awake; then
    adb shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
    sleep 0.6
  fi
  adb shell wm dismiss-keyguard >/dev/null 2>&1
  sleep 0.4
}

reset_apps () {
  wake_device
  adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1; sleep 0.4
  adb shell am force-stop com.kakao.taxi >/dev/null 2>&1
  adb shell am force-stop com.google.android.apps.bard >/dev/null 2>&1
  adb shell am force-stop com.google.android.googlequicksearchbox >/dev/null 2>&1
  sleep 1.0
  adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1; sleep 0.3
}

# 노드 bounds=[x1,y1][x2,y2]에서 좌표를 뽑아 "x y" 형태로 반환. 못 찾으면 빈문자열.
# tap_node_by_attr <ui_xml> <attr_substring>
tap_node_by_attr () {
  local xml="$1" needle="$2"
  python3 - "$xml" "$needle" <<'PY'
import re, sys
xml_path, needle = sys.argv[1], sys.argv[2]
try:
    xml = open(xml_path, encoding='utf-8').read()
except Exception:
    sys.exit(0)
for m in re.finditer(r'<node[^>]*/>', xml):
    s = m.group(0)
    if needle in s:
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', s)
        if b:
            x = (int(b.group(1)) + int(b.group(3))) // 2
            y = (int(b.group(2)) + int(b.group(4))) // 2
            print(f"{x} {y}")
            break
PY
}

# device_info: serial / android version / kakao taxi version 출력 (JSON 한 줄)
device_info_json () {
  local serial android kakao gemini
  serial=$(adb get-serialno 2>/dev/null | tr -d '\r')
  android=$(adb shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')
  kakao=$(adb shell dumpsys package com.kakao.taxi 2>/dev/null \
            | grep -m1 versionName | sed 's/.*versionName=//' | tr -d '\r')
  gemini=$(adb shell dumpsys package com.google.android.apps.bard 2>/dev/null \
            | grep -m1 versionName | sed 's/.*versionName=//' | tr -d '\r')
  python3 -c "
import json
print(json.dumps({
  'serial': '$serial',
  'android': '$android',
  'kakao_taxi_version': '$kakao',
  'gemini_version': '$gemini',
}, ensure_ascii=False))
"
}
