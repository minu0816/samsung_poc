#!/usr/bin/env bash
# Gemini(S26) 자연어 → 앱 자동화 테스트 러너 v2.
#   $1 id  $2 prompt  $3 max_obs_sec(기본 200)
set -uo pipefail
ID="${1:?id}"; PROMPT="${2:?prompt}"; MAX_OBS="${3:-200}"
ROOT="/Users/minu/Workingdir/KG/output/com.kakao.taxi/eval_runs/gemini/${ID}"
rm -rf "$ROOT"; mkdir -p "$ROOT"

START=$(date +%s)
shot () { adb exec-out screencap -p > "$1"; }
focus () { adb shell dumpsys window 2>/dev/null | grep -m1 mCurrentFocus | sed 's/.*u0 //' | sed 's/}$//' | tr -d '\r'; }
ui () { adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1 && adb pull /sdcard/ui.xml "$1" >/dev/null 2>&1; }

# ---------- reset ----------
echo "[${ID}] reset"
# Gemini 작업 중지 다이얼로그 처리 (이전 테스트 잔여물)
adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1; sleep 0.5
adb shell am force-stop com.kakao.taxi >/dev/null 2>&1
adb shell am force-stop com.google.android.apps.bard >/dev/null 2>&1
adb shell am force-stop com.google.android.googlequicksearchbox >/dev/null 2>&1
sleep 1.5
adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1; sleep 0.5
adb shell ime set com.android.adbkeyboard/.AdbIME >/dev/null 2>&1; sleep 0.4

# ---------- launch Gemini ----------
echo "[${ID}] launch"
adb shell am start -n com.google.android.apps.bard/.shellapp.BardEntryPointActivity >/dev/null 2>&1
sleep 3
shot "$ROOT/00_open.png"

# Gemini Live promo dialog '아니요' (있을 때)
ui "$ROOT/ui_open.xml"
DISMISS=$(python3 -c "
import re
xml=open('$ROOT/ui_open.xml').read()
for m in re.finditer(r'<node[^>]+/>', xml):
    s=m.group(0)
    if re.search(r'text=\"아니요\"', s):
        b=re.search(r'bounds=\"\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]\"', s)
        if b:
            x=(int(b.group(1))+int(b.group(3)))//2; y=(int(b.group(2))+int(b.group(4)))//2
            print(x, y); break
")
if [ -n "$DISMISS" ]; then
  echo "[${ID}] dismiss promo $DISMISS"
  adb shell input tap $DISMISS >/dev/null; sleep 1
fi

# ---------- input ----------
echo "[${ID}] tap input"
adb shell input tap 478 1958 >/dev/null
sleep 1.2
echo "[${ID}] broadcast"
adb shell "am broadcast -a ADB_INPUT_TEXT --es msg '$PROMPT'" >/dev/null
sleep 1.0
shot "$ROOT/01_typed.png"

# send 버튼 좌표 (확인됨: 978,2048). UI에서 위치 변동 시 fallback.
ui "$ROOT/ui_typed.xml"
SEND=$(python3 -c "
import re
xml=open('$ROOT/ui_typed.xml').read()
for m in re.finditer(r'<node[^>]+/>', xml):
    s=m.group(0)
    if 'content-desc=\"보내기\"' in s:
        b=re.search(r'bounds=\"\\[(\\d+),(\\d+)\\]\\[(\\d+),(\\d+)\\]\"', s)
        if b:
            x=(int(b.group(1))+int(b.group(3)))//2; y=(int(b.group(2))+int(b.group(4)))//2
            print(x, y); break
")
[ -z "$SEND" ] && SEND="978 2048"
echo "[${ID}] send tap $SEND"
adb shell input tap $SEND >/dev/null
SUBMIT=$(date +%s)
sleep 0.8
shot "$ROOT/02_submitted.png"

# ---------- observe ----------
TRACE="$ROOT/trace.tsv"
echo -e "t_sec\tactivity" > "$TRACE"
KAKAO_FIRST=""
KAKAO_DEEPEST=""
DONE_AT=""
last_shot=-1
while :; do
  t=$(($(date +%s) - SUBMIT))
  [ "$t" -ge "$MAX_OBS" ] && break
  act=$(focus)
  echo -e "${t}\t${act}" >> "$TRACE"
  case "$act" in
    com.kakao.taxi*)
      [ -z "$KAKAO_FIRST" ] && { KAKAO_FIRST=$t; shot "$ROOT/kakao_first_${t}s.png"; }
      KAKAO_DEEPEST="$act"
      ;;
  esac
  if [ $((t - last_shot)) -ge 5 ]; then
    shot "$ROOT/shot_${t}s.png"
    last_shot=$t
  fi
  # "작업 마무리" 도달 검출 — Gemini overlay UI에 노출
  if [ $((t % 5)) -eq 0 ] && [ "$t" -ge 30 ]; then
    ui /tmp/ui_check.xml
    if grep -q '작업 마무리' /tmp/ui_check.xml 2>/dev/null; then
      DONE_AT=$t
      echo "[${ID}] '작업 마무리' detected at +${t}s"
      cp /tmp/ui_check.xml "$ROOT/ui_done.xml"
      shot "$ROOT/done_${t}s.png"
      sleep 2
      shot "$ROOT/done_after_${t}s.png"
      break
    fi
  fi
  sleep 1
done

shot "$ROOT/99_final.png"
ui "$ROOT/ui_final.xml"

# Gemini 응답 텍스트 추출 (마지막 응답 노드)
RESP=$(python3 -c "
import re
xml=open('$ROOT/ui_final.xml').read()
texts=[]
for m in re.finditer(r'<node[^>]+text=\"([^\"]+)\"', xml):
    t=m.group(1)
    if any(kw in t for kw in ['택시','호출','도착','출발','준비','완료','선택','벤티','블랙','모범','예약']):
        texts.append(t)
import json; print(json.dumps(texts, ensure_ascii=False))
")

cat > "$ROOT/meta.json" <<EOF
{
  "id": "$ID",
  "prompt": $(python3 -c "import json; print(json.dumps('$PROMPT', ensure_ascii=False))"),
  "submit_offset_sec": $((SUBMIT - START)),
  "kakao_first_sec_after_submit": ${KAKAO_FIRST:-null},
  "done_at_sec_after_submit": ${DONE_AT:-null},
  "kakao_deepest_activity": $(python3 -c "import json; print(json.dumps('$KAKAO_DEEPEST', ensure_ascii=False))"),
  "extracted_response_texts": $RESP
}
EOF

# 다음 테스트를 위한 cleanup: dialog 처리 + Gemini 종료
adb shell input keyevent KEYCODE_HOME >/dev/null 2>&1
sleep 1
echo "[${ID}] done. submit=+0 kakao_first=+${KAKAO_FIRST:-NA}s done=+${DONE_AT:-NA}s deepest=$KAKAO_DEEPEST"
