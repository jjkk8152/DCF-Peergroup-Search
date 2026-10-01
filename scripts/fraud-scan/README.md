# fraud-scan — 공시 원문 기반 자금부정·내부통제 사례 수집

공시 **제목**이 아니라 **원문(본문 + 첨부)** 에서 자금부정 사건, 자금 관련 통제 미비, 사고 후 개선통제를 찾아
감사에 쓸 수 있는 구조화 데이터(근거문장 포함)로 만든다.

```
collect.ts (LLM 없음, 싼 연산)                                classify.ts (LLM)
[1] list.json 공시목록·rcept_no                              [7] 후보 context만 Claude로 분류
[2] document.xml 원본 ZIP (PK 시그니처로 판별, 디스크 캐시)      - structured outputs (스키마 강제)
[3] ZIP 해제 (xml/html/txt, 인코딩 자동 판별)                    - evidence_text 원문 대조 검증
[4] 정제: script/style/태그 제거, 표 행 보존 → chunk            [8] classified.jsonl / classified.csv
    (heading / paragraph / table_row + section 경로)
[5] fraud·cash·control 조합 채점 (keywords.json)
[6] 근거 chunk ± 2 chunk 문맥, hash 중복 제거, 정정 관계
    → candidates.jsonl / candidates.csv / filings.csv
```

## 빠른 시작

```bash
# .env 에 DART_API_KEY=... (기존 OPENDART_API_KEY 도 인식), 분류 단계는 ANTHROPIC_API_KEY=...

# 1) 감사 대상 회사들의 기간 내 전체 공시(정기·주요사항·기타·외부감사·거래소) 원문 스캔
npx tsx scripts/fraud-scan/collect.ts --companies 대상.txt --from 20240101 --to 20251231 --out fraud-scan-output/대상

# 2) 보낼 프롬프트 미리보기(호출 없음) → 실제 분류
npx tsx scripts/fraud-scan/classify.ts --in fraud-scan-output/대상 --dry-run
npx tsx scripts/fraud-scan/classify.ts --in fraud-scan-output/대상 --min-score 5 --latest-only
```

시장 전체는 공시 수가 너무 많다(거래소공시만 분기 1만 건 이상). 유형·제목으로 1차 범위를 좁힌다.

```bash
# 거래소공시 중 제목에 횡령/배임 — 사건 공시 원문(사고내용·금액·대책 표)까지 파싱
npx tsx scripts/fraud-scan/collect.ts --market --types I --title "횡령|배임" --from 20250101 --to 20251231 --out fraud-scan-output/횡령공시
# 특정 월 제출 사업보고서 전수 (내부회계·자금부정통제 서술 스캔) — --max-docs 로 상한 조정
npx tsx scripts/fraud-scan/collect.ts --market --detail-types A001 --from 20260301 --to 20260331 --max-docs 3000 --out fraud-scan-output/사업보고서_2026_03
# 실제 응답 확인용 소량 E2E (접수번호 직접 지정)
npx tsx scripts/fraud-scan/collect.ts --rcept 20250312900111,20250320000222 --out fraud-scan-output/sample
```

중단(일일 한도 020, 네트워크 등) 후 **같은 명령을 다시 실행하면 이어서** 처리한다. 다운로드한 ZIP은
`fraud-scan-output/_cache/zip` 에 남으므로 `keywords.json` 을 고친 뒤 `--rescan` 하면 재다운로드 없이 재채점된다.

## 1차 채점 (keywords.json)

| 조건 | 점수 |
|---|---|
| 같은 chunk에 fraud + cash | +3 |
| 같은 chunk에 fraud + control | +3 |
| 같은 chunk에 cash + control | +2 |
| 위 조합이 ±2 chunk 안에서 성립 (한쪽은 해당 chunk에 있어야 함) | 위 점수 × 0.5 |
| 보너스 (조합이 있을 때만): 횡령·배임·자금유용 +4, 중요한 취약점 +3, 통제미비·유의한 미비점 +2, 개선조치 +1 | |
| 감점: 정기 자금부정통제 공시 문구("횡령 등 자금 부정을 예방하기 위해" 등)인데 사건·미비 신호가 없음 | −4 |

- 단일 키워드만으로는 0점. 기본 임계값 `--min-score 5`. 제목(heading) chunk는 근거가 될 수 없고 문맥에만 들어간다.
- `exclude`: "부정적/부정확", "유용한/유용성" 같은 오탐 표현 제외.
- `category_hints`: A/B/C 규칙 힌트(참고용). 최종 판정은 classify 단계.
- "당사는 자금 집행에 대한 내부통제를 운영하고 있습니다." → 2점(탈락),
  "전 대표이사가 회사 명의 계좌에서 자금을 무단 출금…내부통제 미비를 확인" → 8점.

## 출력

**candidates.csv / .jsonl** (근거 hash 기준 중복 제거 — 원공시·정정공시·첨부에 같은 문단이 있으면 최신본을 대표로, 나머지는 `duplicates`)
: corp_name, stock_code, rcept_no, rcept_dt, report_nm, document_name(문서명), document_file(ZIP 내 파일명), section,
  score, score_reasons, category_hints, matched_keywords, evidence_text, context, is_latest, is_amendment,
  amends_rcept_no, original_rcept_no, duplicates, source, source_url

**filings.csv**: 공시별 처리 상태(ok/error/pending), 문서·chunk·후보 수, 정정 관계(is_amendment, amends_rcept_no,
original_rcept_no, is_latest, has_later_amendment = list.json `rm`의 "정"), 오류 메시지.

**classified.csv / .jsonl** — 요청 스키마 + 추적용 필드

| 필드 | 내용 |
|---|---|
| corp_name, stock_code, rcept_no, report_nm, rcept_dt, document_name, section | 출처 |
| is_relevant, case_category | A_fraud_incident / B_control_deficiency / C_remediation / none |
| fraud_type, occurrence_year, actor, amount(원), cash_method, related_account | 사건 정보 |
| control_deficiency, control_type(preventive/detective/both), remediation, audit_implication | 통제 정보 |
| evidence_text, **evidence_verified** | 근거문장(원문 그대로) + 원문 대조 결과. 불일치면 confidence=low |
| matched_keywords, rule_score, risk_score(0~10), confidence, rationale | 판단 정보 |
| source, source_url, is_latest, candidate_id, model | 추적 |

## 기존 코드에서 재사용한 것 / 바꾼 것

| 기존 | 처리 |
|---|---|
| `src/services/opendart/constants.ts` DART_API_BASE | 그대로 사용 |
| `document-parser.ts` withRetry·document.xml 다운로드·`stripToPlain` | 패턴만 참고. 사업의 내용 섹션 전용·`<1KB` 판별이라 일반 공시엔 부적합 → `lib/dart.ts`(ZIP 시그니처·DART 오류 XML 상태코드별 처리·캐시), `lib/document.ts`(chunk화)로 새로 작성 |
| `extract-fund-fraud-control.ts` 의 인코딩 판별·회사 식별·헤더 행 처리 | `lib/companies.ts` 로 옮겨 두 스크립트가 공유 |
| 같은 파일의 섹션 번호 체계 판별(headingLevel) | `lib/document.ts` 로 일반화(section 경로 추적) |
| `summarize-peer-snapshot.ts` 의 `claude -p` 호출 | 쓰지 않음 — 분류는 스키마 강제가 필요해 Anthropic SDK structured outputs 사용 |

## 주의

- 이 저장소의 개발 환경에서는 DART에 접속할 수 없어, 실제 응답이 아니라 **공식 응답 형식으로 만든 모의 응답**
  (사건 공시 표, 사업보고서+EUC-KR 첨부, 정정공시, HTML 감사보고서, 깨진 ZIP, 014 오류)으로 E2E 검증했다.
  처음엔 `--rcept` 로 실제 공시 몇 건을 돌려 `filings.csv`·`candidates.csv` 를 눈으로 확인할 것.
- 분류 비용(대략): 후보 1건당 입력 2~3천 토큰 + 출력·사고. 기본 모델 claude-opus-5-5 기준 수 센트 수준이며
  `--effort low`, `--min-score`, `--latest-only`, `--limit` 으로 조절한다. 거절(refusal) 시 서버 측 fallback(`fallbacks: "default"`)을 켜 두었다.
- 공시 원문은 공개 정보지만 분류 단계에서 Anthropic API로 전송된다. 실명은 actor 필드에 쓰지 않도록 지시했다.
