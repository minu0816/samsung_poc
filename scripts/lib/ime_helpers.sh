#!/usr/bin/env bash
# AdbIME(한글 input broadcast) 헬퍼.

ADBIME_ID="com.android.adbkeyboard/.AdbIME"

set_adbime () {
  # Galaxy(Android 12+)는 enable 없이는 set이 무시됨
  adb shell ime enable "$ADBIME_ID" >/dev/null 2>&1
  adb shell ime set "$ADBIME_ID" >/dev/null 2>&1
  sleep 0.5
}

# broadcast_text "text" — AdbIME 인텐트로 한글 포함 텍스트 입력
broadcast_text () {
  local txt="$1"
  # single quote escape
  local safe
  safe=$(printf '%s' "$txt" | sed "s/'/'\\\\''/g")
  adb shell "am broadcast -a ADB_INPUT_TEXT --es msg '${safe}'" >/dev/null 2>&1
}

restore_default_ime () {
  adb shell ime reset >/dev/null 2>&1
}
