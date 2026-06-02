---
name: make-testcases
description: >
  Galaxy S26 화면 자동화 테스트용 testcases.json을 자연어 중괄호 템플릿에서 생성한다.
  사용자가 "{서울역}에서 {강남역} 가는 {벤티/블랙} 택시 불러줘" 처럼 {중괄호} 템플릿으로 테스트를
  요청하거나, testcases.json / 테스트 케이스 / 테스트 스위트를 만들어 달라고 할 때 사용한다.
  반복 횟수·전체 케이스 수 같은 빠진 정보는 사용자에게 물어보고, 택시 위치는 수도권으로 채운다.
---

# make-testcases — 자연어 템플릿 → testcases.json

목적: 사용자의 자연어 중괄호 템플릿을 `eval.run_suite`가 읽는 `testcases.json`(repo 루트)으로 변환한다.
산출물은 `python3 -m eval.run_suite testcases.json` 으로 바로 실행 가능해야 한다.

## 1. 템플릿 파싱
예: `{서울역}에서 {부산역} 가는 {벤티/블랙} 택시 불러줘`
- `{...}` 안 = 파라미터 **값**. 내부에 `/`가 있으면 **여러 값**(각각 다른 케이스가 됨).
- 각 중괄호를 의미 역할(named placeholder)로 변환:
  - `…에서` 앞 → `origin`
  - `…가는` / `…가` 앞 → `destination`
  - 차종어(벤티/블랙/일반/블루/모범/프리미엄/반려동물) → `taxi_type`
  - 시간어(오전/오후/내일/모레/새벽/저녁/N시/N분/예약시간) → `reservation_time`
- 변환된 template은 `{origin}에서 {destination} 가는 {taxi_type} 택시 …` 꼴.

## 2. 시나리오(scenario) 결정
- "예약/예약해줘/예약 잡아줘" 포함 → `taxi_reserve` (reservation_time 필요)
- "대리" → `designated_driver`
- "불러줘/호출/호출해줘/잡아줘" (예약 아님) → `taxi_call`
- phrasing_verb는 문장의 동사(불러줘/예약해줘 등)로 채운다.

## 3. 빠진 정보는 AskUserQuestion으로 반드시 질문
다음이 명확하지 않으면 추측하지 말고 물어본다:
- **전체 케이스 수** (서로 다른 케이스 몇 개를 만들지)
- **케이스당 반복 횟수**(repeat)
- (선택) max_obs_sec — 기본 600(최대 10분)
- 조합 방식이 모호하면: 기본은 `zip`(서로 다른 N개). 사용자가 "모든 조합"을 원하면 `product`.
질문 후 "총 N×repeat 런" 임을 명확히 알려준다.

## 4. 수도권 위치 규칙 (중요)
- 블랙/벤티 택시는 지방 미지원 → `origin`/`destination`은 **서울/경기·인천 수도권**으로 설정.
- 사용자가 지방(예: 부산역, 대구, 광주, 대전 등)을 넣으면 **경고**하고 수도권으로 교체를 제안한다.
- 케이스 수가 시드(템플릿이 준 값)보다 많아 자동 생성할 때는 아래 풀에서 출발/도착 쌍을 뽑는다.
  - 같은 (origin,destination) 쌍 중복 금지, origin ≠ destination.
  - 서울: 서울역, 강남역, 잠실역, 여의도, 홍대입구역, 신촌역, 사당역, 건대입구역, 왕십리역, 용산역, 고속터미널역, 신논현역, 양재역, 노원역, 수서역
  - 경기·인천: 수원역, 광교중앙역, 판교역, 정자역, 인천공항, 부천역, 안양역, 성남시청, 일산, 평촌역

## 5. taxi_type / reservation_time
- `taxi_type`: 사용자가 준 값(예: 벤티/블랙)을 케이스마다 **번갈아(zip이면 순환)** 배치한다.
- `taxi_reserve`면 `reservation_time`을 **10분 단위(:00/:10/:20/:30/:40/:50)** 로만 생성한다.
  오전/오후·시간대를 다양하게 분포(예: 내일 오전 9시, 내일 오후 2시 20분 …).

## 6. testcases.json 작성 (zip 예시)
```json
{
  "suite": "<짧은_이름>",
  "repeat": <R>,
  "max_obs_sec": 600,
  "groups": [
    {
      "scenario": "<taxi_call|taxi_reserve|designated_driver>",
      "combine": "zip",
      "template": "{origin}에서 {destination} 가는 {taxi_type} 택시 불러줘",
      "phrasing_verb": "<불러줘|예약해줘|...>",
      "params": {
        "origin":      ["...N개..."],
        "destination": ["...N개..."],
        "taxi_type":   ["...N개(번갈아)..."]
        /* taxi_reserve면 추가: "reservation_time": ["...N개(10분단위)..."] */
      }
    }
  ]
}
```
규칙:
- zip 모드에서는 모든 params 리스트 길이가 **N(케이스 수)** 또는 1(broadcast)이어야 한다.
- repo 루트 `testcases.json`에 쓴다. **기존 파일이 있으면 덮어쓰기 전에 사용자에게 확인.**
- 여러 시나리오를 섞고 싶으면 `groups`에 그룹을 여러 개 둔다.

## 7. 검증·안내
- 작성 후 `python3 -m eval.run_suite testcases.json --dry-run`을 돌려 케이스 목록·프롬프트를 보여주고
  총 런 수(N × repeat)를 확인시킨다.
- 실행 명령 안내: `python3 -m eval.run_suite testcases.json`
- 실행 전 팁: 폰 화면 잠금은 "없음/스와이프"로(자동 잠금 방지), 결과는 `eval_runs/suites/<suite>_<ts>/`.

## 참고
- 형식·러너: `eval/run_suite.py`. 시나리오 정의: `scenarios/*.yaml`(taxi_call/taxi_reserve/designated_driver).
- 채점은 케이스별로 해당 시나리오 규칙을 따른다(예약은 reservation_time 결정위임 등).
