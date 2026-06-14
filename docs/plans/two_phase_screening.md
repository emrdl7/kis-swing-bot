# KIS Swing Bot — 종목 선정 2단계 파이프라인 + 매수 타이밍 가드 플랜

> 종목 선정 2단계 파이프라인 구현 플랜.
> 기존 LLM 토론 구조·매매 실행 경로는 그대로 두고, **선정 타이밍을 분리**하고 **09:00 개장 노이즈**를 방어하는 것이 전부.

---

## 0. 배경

현재 시스템은 매일 **08:50 단일 실행**으로 종목 선정을 수행한다.

- 3 분석 에이전트(Gemini, 각자 R0 pick) + RiskAgent(Codex, devil's advocate) + Moderator(Codex, 합의)
- 토론 결과 → `state/candidates.json` → monitor가 진입 구간 감시 → 09:00부터 시장가 매수

**목표**

1. 토론을 "전일 15:20 선분석 + 당일 08:50 재평가" 2단계로 분리 → 비용/시간 절감, 장마감 수급 간접 반영
2. 09:00~09:05 개장 노이즈 방어를 위한 매수 타이밍 가드 추가

---

## 1. 절대 지켜야 할 제약

- R0/R1 프롬프트, debate_engine 오케스트레이션, LLM cross-fallback은 **그대로**
- 점수 기반 시스템 도입 금지 (토론 합의 유지)
- RiskAgent 역할 분리 금지
- 장마감 매수 전략 도입 금지 (매수는 여전히 09:00 이후)
- Lookahead 우려 없음 (실시간 트레이딩이라 과거 시뮬 해석 적용 안 됨)
- 저녁 분석 실패·공휴일 케이스는 **기존 아침 풀 파이프라인으로 자동 폴백**

---

## 2. 먼저 읽고 구조 파악할 파일

```
src/scripts/run_morning_screen.py      # 현재 08:50 진입점
src/agents/debate_engine.py            # R0/R1/Moderator 오케스트레이션
src/agents/risk_agent.py               # devil's advocate
src/agents/llm_client.py               # Codex/Gemini cross-fallback
src/data/price_fetcher.py              # KIS 가격·재무 데이터
src/data/news_fetcher.py               # 뉴스 fetcher (HOT/OLD 태깅 존재)
src/core/config.py                     # 설정 스키마
src/engine/monitor.py                  # 매수·매도 관리 루프
src/engine/entry_executor.py           # 실제 매수 실행 (buy_market 사용)
state/candidates.json                  # 최종 후보 저장 위치
~/Library/LaunchAgents/com.kis.*.plist # launchd 스케줄
```

**확인 포인트**

- `run_morning_screen.py`가 함수 단위인지 `main()` 모놀리식인지 → 모놀리식이면 **함수 추출 선행**
- `debate_engine.py`의 Moderator 프롬프트가 문자열로 분리되어 있는지 → 재평가 프롬프트 붙일 지점 확인
- `monitor.py`의 매수 진입 루프 위치 (entry 시도하는 곳) → 쿨다운 가드 삽입 지점

---

## 3. 신규/수정 파일 목록

| 파일 | 액션 | 내용 |
|------|------|------|
| `src/scripts/run_evening_prescreen.py` | **신규** | 전일 15:20 진입점. 풀 파이프라인 실행 → `state/evening_candidates.json` |
| `src/scripts/run_morning_screen.py` | 수정 | 시작 시 저녁 파일 로드 시도 → 있으면 "업데이트 모드", 없으면 기존 로직 |
| `src/data/overnight.py` | **신규** | `fetch_overnight_delta()` — 미국장 마감·조간뉴스·NXT 가격 수집 |
| `src/agents/debate_engine.py` | 수정 | `moderator_reevaluate(prelim, delta)` 메서드 추가. 기존 합의 프롬프트는 건드리지 않음 |
| `src/data/news_fetcher.py` | 수정 | 시간창 옵션 추가 (`since_dt` 인자) — 아침엔 06:00 이후 뉴스만 필터링 가능하도록 |
| `src/core/config.py` | 수정 | `evening_prescreen_enabled`, `evening_candidate_n`, `entry_cooldown_until`, `open_gap_abort_pct` 필드 추가 |
| `src/engine/monitor.py` | 수정 | 매수 진입 루프에 쿨다운 가드 + 시초가 갭 게이트 삽입 |
| `src/core/models.py` | 수정 (최소) | `SwingCandidate`에 `ref_price_eod: float` 필드 추가 (저녁 기준가 저장용) — 없어도 되면 스킵 가능 |
| `state/evening_candidates.json` | 신규 런타임 산출물 | `{date, generated_at, prelim_candidates: [...], debate_log: {...}}` |
| `~/Library/LaunchAgents/com.kis.evening_prescreen.plist` | **신규** | 영업일 15:25 실행 (기존 morning_screen plist 복제) |

---

## 4. 단계별 작업 순서

### Step 1. 공통 로직 추출 (리팩토링 선행)

`run_morning_screen.py`를 다음 함수로 분해 (이미 분해되어 있다면 생략):

- `build_universe() -> list[str]`
- `fetch_market_context(symbols) -> dict` (가격·재무·뉴스 묶음)
- `run_full_debate(context) -> DebateResult`
- `persist(path, result)`

저녁/아침 양쪽에서 재사용하기 위함.

### Step 2. `run_evening_prescreen.py` 작성

- 위 공통 함수 호출 → `run_full_debate()` 실행
- 후보 수를 `evening_candidate_n`(기본 15)로 여유 있게 뽑음
- 저장 경로: `state/evening_candidates.json`
- 실행 파일 락: 기존 `state/rescreen.lock` 패턴 재사용
- 휴장일 가드: 이미 어딘가에 있을 `is_trading_day()` 재사용 (없으면 KIS 달력 API)
- **저장 스키마**에는 각 종목의 장마감 기준가(`ref_price_eod`)를 반드시 포함 → 아침 갭 게이트에서 재사용

### Step 3. `overnight.py` 작성

```
fetch_overnight_delta(prev_close_dt, now_dt) -> dict
```

반환 필드:

- `us_market`: S&P500·나스닥 전일 종가 대비 %. yfinance 최소 구현. 실패 시 None.
- `fresh_news`: `news_fetcher.fetch(since_dt=06:00_today)` — 초벌 후보 종목만 대상
- `nxt_prices`: 초벌 후보 각각의 NXT 현재가 (기존 `price_fetcher`에 NXT 호출 함수 재사용)

실패 시 빈 dict + Moderator에 "델타 없음"으로 표시. 절대 raise 하지 말 것.

### Step 4. Moderator 재평가 메서드

`debate_engine.py`에 추가:

```
def moderator_reevaluate(prelim_result, overnight_delta) -> FinalCandidates
```

프롬프트 뼈대 (한국어):

```
[전일 선정 초벌 후보]
{prelim_result.candidates + 각 종목별 근거 요약}

[밤사이 변화]
- 미국 시장 마감: {delta.us_market}
- 조간 뉴스 (06:00 이후): {delta.fresh_news}
- NXT 프리장 가격: {delta.nxt_prices}

판정 기준:
1. 초벌 후보 중 악재/갭다운으로 진입 부적합한 종목은 제외
2. 과도한 갭업(+5% 이상)은 신뢰도 감점
3. 초벌에 없던 종목을 새로 추가하지 말 것 (유니버스 확장 금지)
4. 선정된 종목만 최종 candidates.json에 남긴다

출력: 기존 moderator 스키마와 동일
```

- LLM 호출은 Codex 1회, Gemini fallback은 기존 `llm_client` 그대로
- R0/R1은 실행 안 함 (비용 절감의 핵심)

### Step 5. `run_morning_screen.py` 분기 로직

```python
prelim = load_evening_candidates()
if prelim and prelim.date == today:
    delta = fetch_overnight_delta(prelim.generated_at, now)
    final = moderator_reevaluate(prelim, delta)
    persist("state/candidates.json", final)
    log("morning update mode")
else:
    # 기존 풀 파이프라인 (폴백)
    context = fetch_market_context(build_universe())
    result = run_full_debate(context)
    persist("state/candidates.json", result)
    log("morning full mode (fallback)")
```

### Step 6. 매수 타이밍 가드 (노이즈 방어)

**6-1. Config 추가** (`src/core/config.py`):

```python
entry_cooldown_until: str = "09:05"   # HH:MM. 이 시각 이전엔 매수 금지
open_gap_abort_pct: float = 3.0       # 시초가 vs 저녁 기준가 절대 이탈이 이 % 이상이면 ABORT
```

**6-2. `monitor.py` 수정**:

매수 시도 루프 진입 직전에 2개 가드 삽입.

**가드 A — 쿨다운**:

```python
now = datetime.now()
cutoff = now.replace(
    hour=int(cfg.entry_cooldown_until.split(":")[0]),
    minute=int(cfg.entry_cooldown_until.split(":")[1]),
    second=0, microsecond=0,
)
if now < cutoff:
    # 가격 관찰만 하고 매수 스킵
    return
```

**가드 B — 시초가 갭 게이트** (쿨다운 해제 직후 1회만 실행):

```python
# 09:05에 각 candidate에 대해:
open_px = kis.get_open_price(candidate.symbol)  # 오늘 시가
ref_px = candidate.ref_price_eod               # 저녁 기준가 (업데이트 모드일 때만 존재)
if ref_px and open_px:
    gap_pct = (open_px - ref_px) / ref_px * 100
    if abs(gap_pct) >= cfg.open_gap_abort_pct:
        log.warning("[%s] 시초가 갭 %.1f%% 이탈 → 당일 진입 포기", candidate.symbol, gap_pct)
        mark_candidate_aborted(candidate.symbol)  # candidates.json에서 당일 삭제 or 플래그
        continue
```

**플래그 처리**: candidate에 `aborted_today: bool` 또는 `state/candidates.json`에서 제거. 기존 후보 제거 로직 재사용.

**6-3. 폴백 경로**:

- 아침이 full mode이면 `ref_price_eod`가 없음 → 갭 게이트 스킵, 쿨다운만 적용
- NXT 선진입은 이번 차수에서 **구현하지 않음** (config flag만 심어둬도 OK)

### Step 7. launchd plist

기존 `com.kis.morning_screen.plist`를 복제:

- `Label` → `com.kis.evening_prescreen`
- `ProgramArguments`의 스크립트 경로 → `run_evening_prescreen`
- `StartCalendarInterval`: Hour=15, Minute=25, Weekday=1-5
- `StandardOutPath`/`StandardErrorPath`는 `logs/evening_prescreen.log`

로드:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kis.evening_prescreen.plist
```

### Step 8. 수동 검증

```bash
# 저녁 분석 강제 실행
python -m src.scripts.run_evening_prescreen --dry-run

# state/evening_candidates.json 생성 확인
cat state/evening_candidates.json | jq .

# 아침 업데이트 모드 강제 트리거
python -m src.scripts.run_morning_screen --debug

# 로그에 "morning update mode" 확인
# state/candidates.json 갱신 확인

# 쿨다운 가드 단위 확인 (시계 mock 가능하면)
# 시초가 갭 게이트: 인위적으로 ref_price_eod를 현재가 대비 5% 낮게 세팅 후 로그 확인
```

---

## 5. 수용 기준 (acceptance)

- [ ] `run_evening_prescreen.py` 단독 실행 시 `state/evening_candidates.json` 생성 (date/generated_at/prelim 포함)
- [ ] 아침 실행 시 저녁 파일 있으면 Moderator 1회만 호출 (로그로 확인 — LLM 호출 카운트)
- [ ] 저녁 파일 날짜가 어제(전일)면 무시하고 폴백 모드
- [ ] 저녁 파일 삭제 후 아침 실행 시 기존 동작과 100% 동일
- [ ] 초벌 후보에 없던 종목은 최종 candidates에 절대 등장하지 않음
- [ ] 갭업 +5% 이상 종목이 초벌에 있을 때 모더레이터가 감점하거나 제외하는 로그 확인
- [ ] **monitor가 09:05 전엔 매수 시도하지 않음** (로그로 확인)
- [ ] **시초가가 저녁 기준가 대비 ±3% 이상 이탈한 종목은 매수 스킵** (로그로 확인)
- [ ] 기존 `monitor`의 매도 로직, `entry_executor`, 대시보드 로직은 **변경되지 않음**

---

## 6. 데이터 타이밍 경계 (섞이지 않게)

| 구분 | 수집 시점 | 사용처 |
|------|----------|--------|
| `previous_day_closed` | 당일 09:00~15:20 확정 데이터 | Phase A (저녁 토론) |
| `overnight_delta` | 15:20~08:45 사이 발생 | Phase B (아침 재평가)만 |
| `open_context` | 09:00~09:05 시초가 | 매수 타이밍 가드 (갭 게이트) |
| `intraday` | 09:05 이후 | 기존 monitor (변경 없음) |

---

## 7. 하지 말 것

- R0/R1 프롬프트 수정 금지
- 점수 기반 시스템 도입 금지 (토론 합의 유지)
- RiskAgent 역할 분리 금지
- 장마감 매수 로직 추가 금지 (매수는 여전히 09:05 이후)
- 유니버스를 아침에 확장하지 말 것 (초벌 후보 subset만 유지)
- NXT 선진입·ORB·분할 매수 구현 금지 (이번 차수 범위 밖)

---

## 8. 기대 효과

- LLM 비용 ≈ 40% 감소 (아침엔 Moderator 1회만 호출)
- 아침 실행 시간 단축 (풀 토론 → 재평가만)
- 장마감 수급·섹터 강도를 간접 반영 (저녁 토론에서 당일 종가까지 관찰)
- 조간 뉴스·미국장 갭 리스크는 Moderator 재평가에서 포착
- **09:00~09:05 동시호가 노이즈·V자 반등 함정 회피**
- **시초가 과도 갭 종목 자동 필터링** → 고점 물림 방지

---

## 9. 리스크 및 완화

| 리스크 | 완화 |
|--------|------|
| 저녁 분석 후 밤사이 대형 악재 → 초벌 후보 전부 부적합 | Moderator 재평가가 전부 제외하면 `candidates.json`이 비어 monitor가 매수 안 함 (정상) |
| 미국장 데이터 수집 실패 | `fetch_overnight_delta`는 실패해도 빈 dict 반환. Moderator는 "델타 없음" 상태로 기존 초벌 채택 |
| 09:05 쿨다운이 너무 길어 급등 종목 놓침 | `entry_cooldown_until` 설정값이므로 운영하며 09:02 등으로 조정 가능 |
| ±3% 갭 게이트가 너무 타이트 | `open_gap_abort_pct` 설정값. 운영하며 조정 |
| 저녁 실행 후 monitor가 해당 시각에 죽어있음 | 별도 launchd plist로 독립 실행. morning_screen plist와 무관 |

---

## 10. 향후 확장 (이번 차수 밖, 메모만)

- NXT 유동성 있는 종목 08:55 선진입 (별도 플래그)
- 시초가 갭 관찰 후 ORB 돌파 확인 매수 (09:05~09:30 레인지 돌파)
- 분할 매수 (50% 09:05 / 50% 09:20 VWAP 근접)
- 섹터 강도 지수·외국인 수급 데이터를 저녁 토론 컨텍스트에 주입
