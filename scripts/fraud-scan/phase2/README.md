# Phase 2 — External Fraud Benchmarking & RCM Coverage (감사인용)

외부감사인의 ISA 240 / ISA 240 (Revised) fraud risk assessment 및 audit response 를 지원하는 로컬 도구.
회사 내부통제 컨설팅 도구가 아니며, 감사인의 전문가적 판단을 대체하지 않는다.

```
External Fraud Benchmarking → Fraud Scenario Identification → RCM Coverage Assessment
→ Control Gap Identification → Auditor Follow-up → Suggested Audit Procedures
```

## 아키텍처

```
[Phase 1 — TypeScript, 변경 없음]                        [Phase 2 — Python (이 폴더)]
scripts/fraud-scan/collect.ts  ◀── subprocess ──────── phase1_bridge.run_collect (선택 peer corp_code·기간)
  list.json → document.xml ZIP → chunk → 채점               │ import_output: filings.csv / candidates.jsonl → SQLite
  → candidates.jsonl / filings.csv  ─────────────────────▶ │
data/peer-snapshot, valuation-cache ─────────────────────▶ peer_selector (결정론 유사도 + 이유, 감사인 체크박스 확정)
                                                         fraud_case_engine   실제 사례 / 통제미비 공시 / 단순 hit 구분, 6절 스키마
                                                         fraud_scenario_engine  FS001~FS010 분류·clustering, 신규 후보
                                                         rcm_parser          업로드·헤더 탐지·자동 컬럼 매핑·원본 보관
                                                         rcm_mapper          12요소 매칭(통제 기술에서만 통제요소 인정)
                                                         contradiction_engine 상충 진술 탐지(진위 판단 안 함)
                                                         coverage_engine     4개 판정 + 가드레일(LLM 상향 금지)
                                                         audit_response_engine Control Gap ↔ Audit Response 분리
                                                         evidence_trace      drill-down 체인 + 파이프라인
                                                         export_excel        10(+1) 시트 workbook
                                                         app.py              Streamlit UI
                                                         llm_client          (선택) Claude, 캐시, 인용 검증
```

LLM 은 **기본 OFF**. 꺼져 있어도 전 과정이 결정론으로 동작한다. 켜면 사례 구조화·coverage 검토 의견에만 쓰이며:
- 후보 문맥 / 관련 RCM 행만 전송(공시 전문·RCM 전체 미전송), 동일 입력은 SQLite `llm_cache` 재사용
- 모든 인용문을 원문과 대조 — 원문에 없는 금액·행위자·통제는 버림
- LLM 은 coverage 를 규칙 결과보다 **유리하게 바꿀 수 없음** (`guardrails.enforce_coverage`)
- 확정적 결론 표현(유의적 위험 확정, 미비 등급, 감사의견 등)은 제거 (`guardrails.sanitize`)

## 실행

```bash
pip install -r scripts/fraud-scan/phase2/requirements.txt
streamlit run scripts/fraud-scan/phase2/app.py          # 윈도우: scripts\fraud-scan\phase2\run_app.bat
python scripts/fraud-scan/phase2/run_sample.py          # 샘플 E2E (DART·LLM 호출 없음) → fraud-scan-output/phase2_sample.{db,xlsx}
python -m pytest scripts/fraud-scan/phase2/tests -q     # 테스트
```

화면 순서: Dashboard · 1 Engagement Profile · 2 Peer Selection · 3 Fraud Cases(Phase 1 수집 실행/가져오기 → 구조화) ·
4 Fraud Scenarios · 5 RCM Upload(컬럼 매핑) · 6 Additional Evidence(상충 정보) · 7 Coverage & Audit Response · 8 Review & Export.
사이드바 "샘플 데이터로 데모 생성"으로 전체 흐름을 바로 볼 수 있다.

## 이미지 첨부 시각 판독 (visual queue)

감사보고서·내부회계관리제도 운영실태보고서가 사업보고서에 **스캔 이미지로만** 첨부되면 API 텍스트로는 내용을 얻을 수 없다.

1. **탐지(자동, 추가 API 호출 없음)** — `visual_queue.py scan`: Phase 1 이 받아 둔 원문 ZIP 캐시를 다시 열어, 대상 문서
   (운영실태·내부회계·평가보고서 = 우선순위 1, 감사·검토보고서 = 2)이면서 이미지 태그가 있고 본문 텍스트가 거의 없는 첨부를
   `visual_queue`(DB v2)에 올린다. 텍스트가 있는 첨부는 Phase 1 이 이미 처리하므로 제외.
2. **판독(맥에서 Claude Code)** — 저장소의 `.claude/skills/dart-visual-extract/SKILL.md` 절차대로 Claude 가 브라우저
   (Claude in Chrome 또는 내장 브라우저)로 뷰어를 열고 첨부를 골라 확대·스크롤하며 관련 문단을 **글자 그대로 전사**해
   `record` 한다(판독 불가 글자는 `[?]`, 근거 문장은 전사 안에서만 발췌 — 도구가 대조). 진행 상태는 todo → in_progress → done_found/done_nothing/not_available/blocked.
3. **연결** — `visual_queue.py import`: 판독 기록을 Phase 1 형식으로 내보내 Phase 2 에 적재하고 사례 구조화까지 실행.
   근거 위치는 `첨부파일 · 운영실태보고서 p.N` 으로 남는다. 앱 '3. Fraud Cases' → '이미지 첨부 판독 목록' 탭에서도 실행·확인 가능.

맥에서 시작: Claude Code 에 "engagement 1 이미지 첨부 판독 진행해줘" → 스킬이 로드되어 scan → next → 판독·record → done 반복 → import.
(선택) `visual_queue.py ocr <파일>` 로 tesseract(kor+eng) 판독과 교차 확인 (`brew install tesseract tesseract-lang poppler`).

## 환경 변수

| 변수 | 용도 |
|---|---|
| `DART_API_KEY` (또는 `OPENDART_API_KEY`) | Phase 1 수집 실행 시 (`.env.local`/`.env` 자동 로드). Node.js 필요 |
| `ANTHROPIC_API_KEY` | 선택 — LLM 보조 기능 |
| `PHASE2_DB` | DB 경로 (기본 `fraud-scan-output/phase2.db`) |
| `PHASE2_MODEL` | 선택 — 기본 `claude-opus-5-5` |

## DB 스키마 (SQLite, `db.py` migration v1)

Phase 1 은 DB 가 없었으므로 기존 데이터 변경 없음(파일 산출물 그대로). 이후 변경은 `MIGRATIONS` 에 버전 추가 방식.

| 테이블 | 내용 |
|---|---|
| engagements | Engagement Profile |
| peer_candidates | 후보·점수·이유, `selected`(감사인 확정)·selected_by/at |
| collection_runs / dart_filings / source_candidates | Phase 1 실행 이력·공시·후보(원본 JSON `raw` 보관) |
| fraud_cases | 6절 스키마 + `case_kind`, `field_evidence`(필드별 근거 문장), `source_evidence`(필수) |
| scenario_library / case_scenarios / scenario_candidates | taxonomy, 사례↔시나리오, New Scenario Candidate |
| rcm_uploads / rcm_rows | 원본 파일 bytes·컬럼 매핑 / 원본 행 JSON + 표준 필드 |
| additional_evidence / contradictions | 인터뷰 등 진술 / Potential Contradiction |
| scenario_control_matches | 상위 5개 통제, 12요소 점수, 요소별 RCM 인용, 모호 사유 |
| coverage_results | 12절 스키마 (+ design_vs_operating_note, method, llm_notes), coverage CHECK 제약 |
| audit_considerations | Why this matters, Control Coverage Gap / Audit Response Consideration 분리, 질문·절차·증거, conclusion |
| llm_cache / review_log / schema_version | LLM 캐시, 검토·선택 이력, migration 버전 |

AI 생성 테이블 공통 컬럼: `ai_generated, reviewer, review_status(Not Reviewed/Accepted/Modified/Rejected), reviewer_comment`.
재분석해도 검토 결과는 보존된다.

## Coverage 판정 규칙 (결정론)

| 조건 | 판정 |
|---|---|
| 관련 위험·통제 기술 없음 | Not Covered |
| 핵심요소(core) 전부가 구체적 통제 기술로 확인, critical 우회경로 대응됨, 상충 정보 없음 | Covered |
| 핵심요소 일부만 확인 / critical 우회경로 미대응 | Partially Covered |
| 핵심요소가 모호한 기술("적절히", 수행자·방법·증빙 없음) 또는 상충 진술로만 확인 | Cannot Determine |
| 일반 통제(예: 일반 지급승인)만 있고 메커니즘 고유 요소 대응 없음 | Not Covered (일반 통제는 근거와 함께 명시) |

통제요소는 **통제 기술**(control description·evidence·owner)에서만 인정한다 — 위험 기술은 통제가 아니다.
판정은 설계(design) 존재 여부이며 운영효과성은 평가하지 않는다.

## 샘플

`samples/` — **모든 회사·공시·접수번호는 가상([SAMPLE])** 이다. `sample_rcm.xlsx`(상단 제목 행 + 국문 컬럼명),
`sample_rcm.csv`, `sample_additional_evidence.csv`(RCM 과 상충하는 인터뷰), `phase1_output/`(Phase 1 형식 후보 8건).

샘플 결과: 후보 8 → 실제 부정 6 / 통제미비 공시 1 / 단순 키워드 1(정기 자금부정통제 문구) → 시나리오 8개 →
Covered 1(FS001) · Partially 6 · Not Covered 1(FS004 특수관계자 자금 유출) · 상충 1건(OTP 보관자 CFO vs Finance Manager →
FS003 의 인증수단 요소는 '상충 정보 — Auditor follow-up required').

## Known limitations

- 결정론 엔진은 용어 사전(scenario_library.json, fraud_case_engine 상수) 기반이다. 표현이 다른 공시·RCM 은 놓칠 수 있다(LLM 보조 또는 사전 보강).
- peer 규모 비교는 캐시에 있는 **시가총액**만 가능 — 매출·자산 기준 입력 시 규모 요소는 제외된다.
- 해외법인 여부는 사업의 개요 문구 기준 추정이다.
- 상충 정보 탐지는 현재 '보관·관리 주체' 유형만 지원한다.
- 이미지 전용 판정은 DART XML 의 `<IMG>`/`<IMAGE>` 태그와 본문 글자 수 기준이다. 실제 공시로 아직 검증하지 못했으므로 첫 `scan` 결과(queued·text_available_targets)를 확인하고 임계값을 조정할 것.
- 시각 판독은 Claude 의 화면 판독(전사)이며 OCR 오류 가능성이 있다 — 금액·직위는 감사인이 원문 이미지로 확인할 것(뷰어 링크·페이지 기록).
- 실제 OpenDART 응답과 실제 Claude API 로는 이 개발 환경에서 테스트하지 못했다(네트워크·키 없음). 샘플·모의 응답으로 검증.
- RCM 범위가 좁으면 'Not Covered' 는 RCM 범위 밖일 가능성을 포함한다(information_needed 에 표시).

## Phase 3 후보

DART 재무제표로 peer 매출·자산 규모 비교 · 인터뷰/문서 업로드(PDF·Word) 진술 자동 추출 · 상충 유형 확장(승인권자·주기·시스템) ·
시나리오 사전의 감사인 승인 워크플로(New Scenario Candidate → taxonomy 편입) · 연도별 비교·롤포워드 · 다중 RCM 버전 비교 ·
Message Batches 로 LLM 비용 절감 · 다중 사용자 검토(서버 배포 시 인증·감사로그).
