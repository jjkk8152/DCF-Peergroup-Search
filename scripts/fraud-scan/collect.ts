/**
 * 공시 원문 기반 자금부정·내부통제 후보 수집 (LLM 없이 싼 연산만)
 *
 *   [1] list.json 공시목록·rcept_no → [2] document.xml 원본 ZIP → [3] ZIP 해제 →
 *   [4] 본문 정제(태그 제거·표 행 보존) → [5] fraud/cash/control 조합 채점 → [6] 근거 chunk ± 문맥
 *
 * 사용법 (npx tsx scripts/fraud-scan/collect.ts …):
 *   대상 회사 전체 공시:  --companies list.txt --from 20240101 --to 20251231 --out fraud-scan-output/감사대상
 *                        (--corp 005930,카카오 로 직접 지정 가능. 기본 유형 A·B·E·F·I 전부)
 *   시장 전체(제목 1차 필터 필수급):
 *                        --market --types I --title "횡령|배임" --from 20250101 --to 20251231
 *                        --market --detail-types A001 --from 20260301 --to 20260331   (사업보고서 전수)
 *   특정 공시 직접:      --rcept 20250310000111,20250401000222
 *
 * 옵션: --types A,B,E,F,I  --detail-types A001,F001  --title <정규식>  --latest-only
 *       --market-cls Y|K|N|E  --min-score 5  --context 2  --keywords <json>  --max-docs 2000
 *       --interval 400(ms)  --relist(목록 재조회)  --rescan(ZIP 캐시로 재채점)
 *
 * 출력(--out): candidates.jsonl / candidates.csv (중복 제거된 후보), filings.csv (공시별 처리 상태·정정 관계),
 *             run.log, 재개용 상태(filings.json, scanned.jsonl, raw-candidates.jsonl). ZIP 캐시는 --cache(기본 공용).
 * 중단(일일 한도 초과 등) 후 같은 명령을 다시 실행하면 처리된 공시는 건너뛰고 이어서 진행한다.
 *
 * 환경변수: DART_API_KEY (없으면 OPENDART_API_KEY). .env.local / .env 자동 로드.
 */
import fs from "fs";
import path from "path";
import { loadEnv, getDartApiKey } from "./lib/env";
import { DartClient, DartDisclosure, DartDocumentError, FatalDartError, ListParams, dartViewerUrl, splitPeriod } from "./lib/dart";
import { parseZip } from "./lib/document";
import { Candidate, DEFAULT_KEYWORDS_PATH, KeywordMatcher, extractCandidates, loadKeywords } from "./lib/scoring";
import { linkAmendments } from "./lib/amendments";
import { HEADER_LIKE_RE, loadCorpCodes, readCompanyList, resolveCompany } from "./lib/companies";
import { appendJsonl, makeLogger, readJsonl, writeCsv, writeJsonl } from "./lib/output";

interface Options {
  companies: string[];
  companiesFromFile: boolean;
  market: boolean;
  rcepts: string[];
  from: string;
  to: string;
  types: string[];
  detailTypes: string[];
  titleRe: RegExp | null;
  latestOnly: boolean;
  marketCls: string | null;
  minScore: number;
  context: number;
  out: string;
  cache: string;
  keywords: string;
  maxDocs: number;
  interval: number;
  relist: boolean;
  rescan: boolean;
}

const ymd = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, "");
const normDate = (s: string) => s.replace(/-/g, "");
const fmtDate = (s: string) => (/^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s);
const splitList = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

function fail(msg: string): never {
  console.error(`❌ ${msg}`);
  process.exit(1);
}

function parseArgs(argv: string[]): Options {
  const today = new Date();
  const o: Options = {
    companies: [],
    companiesFromFile: false,
    market: false,
    rcepts: [],
    from: ymd(new Date(Date.UTC(today.getFullYear() - 1, today.getMonth(), today.getDate()))),
    to: ymd(today),
    types: [],
    detailTypes: [],
    titleRe: null,
    latestOnly: false,
    marketCls: null,
    minScore: 5,
    context: 2,
    out: path.resolve("fraud-scan-output/run"),
    cache: path.resolve("fraud-scan-output/_cache/zip"),
    keywords: DEFAULT_KEYWORDS_PATH,
    maxDocs: 2000,
    interval: 400,
    relist: false,
    rescan: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const v = () => argv[++i] ?? fail(`${a} 값이 없습니다`);
    if (a === "--companies") {
      o.companies.push(...readCompanyList(v()));
      o.companiesFromFile = true;
    } else if (a === "--corp") o.companies.push(...splitList(v()));
    else if (a === "--market") o.market = true;
    else if (a === "--rcept") o.rcepts.push(...splitList(v()));
    else if (a === "--rcept-file") o.rcepts.push(...readCompanyList(v()));
    else if (a === "--from") o.from = normDate(v());
    else if (a === "--to") o.to = normDate(v());
    else if (a === "--types") o.types = splitList(v()).map((t) => t.toUpperCase());
    else if (a === "--detail-types") o.detailTypes = splitList(v()).map((t) => t.toUpperCase());
    else if (a === "--title") o.titleRe = new RegExp(v());
    else if (a === "--latest-only") o.latestOnly = true;
    else if (a === "--market-cls") o.marketCls = v();
    else if (a === "--min-score") o.minScore = Number(v());
    else if (a === "--context") o.context = Number(v());
    else if (a === "--out") o.out = path.resolve(v());
    else if (a === "--cache") o.cache = path.resolve(v());
    else if (a === "--keywords") o.keywords = path.resolve(v());
    else if (a === "--max-docs") o.maxDocs = Number(v());
    else if (a === "--interval") o.interval = Number(v());
    else if (a === "--relist") o.relist = true;
    else if (a === "--rescan") o.rescan = true;
    else fail(`알 수 없는 옵션: ${a}`);
  }
  const modes = [o.companies.length > 0, o.market, o.rcepts.length > 0].filter(Boolean).length;
  if (modes !== 1) fail("--companies/--corp, --market, --rcept 중 정확히 하나를 지정하세요.");
  if (!/^\d{8}$/.test(o.from) || !/^\d{8}$/.test(o.to) || o.from > o.to) fail(`기간이 잘못되었습니다: ${o.from} ~ ${o.to}`);
  if (o.market && !o.types.length && !o.detailTypes.length && !o.titleRe) {
    fail("--market 은 공시 수가 매우 많습니다(거래소공시만 분기 1만 건+). --types/--detail-types/--title 중 하나 이상으로 범위를 좁히세요.");
  }
  if (!Number.isFinite(o.minScore) || !Number.isInteger(o.context) || o.context < 0) fail("--min-score / --context 값이 잘못되었습니다.");
  if (o.companies.length && !o.types.length && !o.detailTypes.length) o.types = ["A", "B", "E", "F", "I"];
  return o;
}

/** [1] 공시목록 수집 */
async function listFilings(o: Options, client: DartClient, log: (m: string) => void): Promise<DartDisclosure[]> {
  if (o.rcepts.length) {
    // 직접 지정: 메타데이터는 접수번호에서 알 수 있는 것만 (접수일 = 앞 8자리)
    return o.rcepts.map((r) => ({ corp_code: "", corp_name: "", stock_code: "", corp_cls: "", report_nm: "", rcept_no: r, flr_nm: "", rcept_dt: r.slice(0, 8), rm: "" }));
  }
  const typeParams: Array<Partial<ListParams>> = o.detailTypes.length
    ? o.detailTypes.map((t) => ({ pblntf_detail_ty: t }))
    : o.types.length
      ? o.types.map((t) => ({ pblntf_ty: t }))
      : [{}];
  const base: Partial<ListParams> = o.latestOnly ? { last_reprt_at: "Y" } : {};
  const queries: ListParams[] = [];

  if (o.market) {
    // 회사 미지정 검색은 기간 3개월 제한
    for (const [b, e] of splitPeriod(o.from, o.to, 3))
      for (const t of typeParams) queries.push({ ...base, ...t, bgn_de: b, end_de: e, ...(o.marketCls ? { corp_cls: o.marketCls } : {}) });
  } else {
    const corpCodes = loadCorpCodes();
    for (const [idx, q] of o.companies.entries()) {
      const corp = resolveCompany(q, corpCodes);
      if (!corp) {
        if (idx === 0 && o.companiesFromFile && HEADER_LIKE_RE.test(q)) log(`↷ 헤더 행으로 보고 건너뜀: ${q}`);
        else log(`❌ corp_code를 찾을 수 없음: ${q}`);
        continue;
      }
      for (const t of typeParams) queries.push({ ...base, ...t, corp_code: corp.corp_code, bgn_de: o.from, end_de: o.to });
    }
  }

  const byNo = new Map<string, DartDisclosure>();
  for (const [i, q] of queries.entries()) {
    const rows = await client.list(q);
    for (const r of rows) byNo.set(r.rcept_no, r);
    log(`[목록 ${i + 1}/${queries.length}] ${q.corp_code ?? "전체"} ${q.pblntf_detail_ty ?? q.pblntf_ty ?? "전유형"} ${q.bgn_de}~${q.end_de}: ${rows.length}건`);
  }
  let filings = [...byNo.values()];
  if (o.titleRe) filings = filings.filter((f) => o.titleRe!.test(f.report_nm));
  return filings.sort((a, b) => a.rcept_no.localeCompare(b.rcept_no));
}

interface RawCandidate extends Candidate {
  rcept_no: string;
  document_file: string;
  document_name: string;
}

interface ScanStatus {
  rcept_no: string;
  status: "ok" | "error";
  documents?: number;
  chunks?: number;
  candidates?: number;
  errors?: string[];
  error?: string;
  at: string;
}

/** [2]~[6] 공시 1건 처리 */
async function scanFiling(f: DartDisclosure, client: DartClient, matcher: KeywordMatcher, o: Options): Promise<{ status: ScanStatus; raws: RawCandidate[] }> {
  const at = new Date().toISOString();
  const buf = await client.document(f.rcept_no);
  let parsed;
  try {
    parsed = parseZip(buf);
  } catch (e) {
    client.evictCache(f.rcept_no); // 전송 중 잘린 ZIP일 수 있으니 재실행 때 다시 다운로드
    return { status: { rcept_no: f.rcept_no, status: "error", error: `ZIP 해제 실패: ${(e as Error).message}`, at }, raws: [] };
  }
  const raws: RawCandidate[] = [];
  let chunkCount = 0;
  for (const doc of parsed.documents) {
    chunkCount += doc.chunks.length;
    for (const c of extractCandidates(doc.chunks, matcher, { minScore: o.minScore, contextChunks: o.context })) {
      raws.push({ ...c, rcept_no: f.rcept_no, document_file: doc.entry, document_name: doc.documentName });
    }
  }
  return {
    status: { rcept_no: f.rcept_no, status: "ok", documents: parsed.documents.length, chunks: chunkCount, candidates: raws.length, errors: parsed.errors, at },
    raws,
  };
}

/** 후보 중복 제거 + 정정 관계 반영 → 최종 산출물 */
function finalize(o: Options, filings: DartDisclosure[], log: (m: string) => void) {
  const scanned = new Map<string, ScanStatus>();
  for (const s of readJsonl<ScanStatus>(path.join(o.out, "scanned.jsonl"))) scanned.set(s.rcept_no, s);
  const seen = new Set<string>();
  const raws = readJsonl<RawCandidate>(path.join(o.out, "raw-candidates.jsonl")).filter((r) => {
    const k = `${r.rcept_no}|${r.document_file}|${r.hash}`; // 중단 후 재처리로 생긴 중복 행 제거
    if (seen.has(k) || scanned.get(r.rcept_no)?.status !== "ok") return false;
    seen.add(k);
    return true;
  });
  const byNo = new Map(filings.map((f) => [f.rcept_no, f]));
  const amend = linkAmendments(filings);

  // 같은 근거문장(hash)은 하나로: 최신본 공시 > 최근 접수 순으로 대표 선정, 나머지는 duplicates로 기록
  const groups = new Map<string, RawCandidate[]>();
  for (const r of raws) {
    if (!groups.has(r.hash)) groups.set(r.hash, []);
    groups.get(r.hash)!.push(r);
  }
  const records = [...groups.values()].map((g) => {
    g.sort((a, b) => Number(amend.get(b.rcept_no)?.is_latest ?? true) - Number(amend.get(a.rcept_no)?.is_latest ?? true) || b.rcept_no.localeCompare(a.rcept_no));
    const r = g[0];
    const f = byNo.get(r.rcept_no);
    const a = amend.get(r.rcept_no);
    return {
      candidate_id: r.hash,
      corp_name: f?.corp_name ?? "",
      stock_code: f?.stock_code ?? "",
      corp_code: f?.corp_code ?? "",
      rcept_no: r.rcept_no,
      rcept_dt: fmtDate(f?.rcept_dt ?? r.rcept_no.slice(0, 8)),
      report_nm: f?.report_nm ?? "",
      document_name: r.document_name,
      document_file: r.document_file,
      section: r.section,
      score: r.score,
      matched_keywords: [...new Set([...r.keywords.fraud, ...r.keywords.cash, ...r.keywords.control])],
      keywords_by_category: r.keywords,
      score_reasons: r.reasons,
      category_hints: r.hints,
      evidence_text: r.evidence,
      context: r.context,
      source: `${r.rcept_no} / ${r.document_name || r.document_file}`,
      source_url: dartViewerUrl(r.rcept_no),
      is_latest: a?.is_latest ?? true,
      is_amendment: a?.is_amendment ?? false,
      amends_rcept_no: a?.amends_rcept_no ?? null,
      original_rcept_no: a?.original_rcept_no ?? r.rcept_no,
      duplicates: g.slice(1).map((d) => ({ rcept_no: d.rcept_no, document_file: d.document_file })),
    };
  });
  records.sort((a, b) => b.score - a.score || b.rcept_dt.localeCompare(a.rcept_dt));

  writeJsonl(path.join(o.out, "candidates.jsonl"), records);
  writeCsv(
    path.join(o.out, "candidates.csv"),
    ["candidate_id", "corp_name", "stock_code", "rcept_no", "rcept_dt", "report_nm", "document_name", "section", "score", "category_hints", "matched_keywords", "score_reasons", "evidence_text", "context", "is_latest", "original_rcept_no", "duplicates", "source_url"],
    records.map((r) => ({ ...r, duplicates: r.duplicates.map((d) => d.rcept_no) })),
  );

  const candCount = new Map<string, number>();
  for (const r of records) candCount.set(r.rcept_no, (candCount.get(r.rcept_no) ?? 0) + 1);
  writeCsv(
    path.join(o.out, "filings.csv"),
    ["corp_name", "stock_code", "corp_code", "rcept_no", "rcept_dt", "report_nm", "status", "documents", "chunks", "candidates", "is_amendment", "amends_rcept_no", "original_rcept_no", "is_latest", "has_later_amendment", "error", "source_url"],
    filings.map((f) => {
      const s = scanned.get(f.rcept_no);
      const a = amend.get(f.rcept_no);
      return {
        ...f,
        rcept_dt: fmtDate(f.rcept_dt),
        status: s?.status ?? "pending",
        documents: s?.documents,
        chunks: s?.chunks,
        candidates: candCount.get(f.rcept_no) ?? 0,
        ...a,
        error: s?.error ?? (s?.errors?.length ? s.errors.join(" / ") : ""),
        source_url: dartViewerUrl(f.rcept_no),
      };
    }),
  );

  const ok = filings.filter((f) => scanned.get(f.rcept_no)?.status === "ok").length;
  const err = filings.filter((f) => scanned.get(f.rcept_no)?.status === "error").length;
  const hintCount: Record<string, number> = {};
  for (const r of records) for (const h of r.category_hints) hintCount[h] = (hintCount[h] ?? 0) + 1;
  log(`\n✅ 공시 ${filings.length}건 (처리 ${ok}, 오류 ${err}, 미처리 ${filings.length - ok - err}) → 후보 ${records.length}건 (중복 제거 전 ${raws.length})`);
  log(`   규칙 힌트: ${Object.entries(hintCount).map(([k, v]) => `${k} ${v}`).join(", ") || "-"}`);
  log(`   출력: ${o.out}${path.sep}{candidates.csv, candidates.jsonl, filings.csv}`);
}

async function main() {
  loadEnv();
  const o = parseArgs(process.argv.slice(2));
  const key = getDartApiKey();
  if (!key) fail("DART_API_KEY(또는 OPENDART_API_KEY)가 없습니다. .env 에 설정하세요.");
  fs.mkdirSync(o.out, { recursive: true });
  const log = makeLogger(path.join(o.out, "run.log"));
  const client = new DartClient(key, { minIntervalMs: o.interval, cacheDir: o.cache, log });
  const matcher = new KeywordMatcher(loadKeywords(o.keywords));

  const filingsPath = path.join(o.out, "filings.json");
  let filings: DartDisclosure[];
  try {
    if (!o.relist && fs.existsSync(filingsPath)) {
      filings = JSON.parse(fs.readFileSync(filingsPath, "utf8"));
      log(`📄 기존 공시목록 재사용 ${filings.length}건 (다시 조회하려면 --relist)`);
    } else {
      filings = await listFilings(o, client, log);
      fs.writeFileSync(filingsPath, JSON.stringify(filings));
    }
  } catch (e) {
    if (e instanceof FatalDartError) fail(`${e.message} — API 키/IP/일일 한도를 확인하세요.`);
    throw e;
  }
  if (filings.length > o.maxDocs) fail(`대상 공시 ${filings.length}건이 --max-docs ${o.maxDocs} 를 넘습니다. 범위를 좁히거나 --max-docs 를 올리세요.`);

  const scannedPath = path.join(o.out, "scanned.jsonl");
  const rawPath = path.join(o.out, "raw-candidates.jsonl");
  if (o.rescan) for (const p of [scannedPath, rawPath]) fs.rmSync(p, { force: true });
  const done = new Set(readJsonl<ScanStatus>(scannedPath).filter((s) => s.status === "ok").map((s) => s.rcept_no));
  const todo = filings.filter((f) => !done.has(f.rcept_no));
  log(`🔎 공시 ${filings.length}건 중 ${todo.length}건 처리 (기간 ${o.from}~${o.to}, 임계점수 ${o.minScore}, 문맥 ±${o.context})`);

  for (const [i, f] of todo.entries()) {
    const tag = `[${i + 1}/${todo.length}] ${f.corp_name || "-"} ${f.report_nm || f.rcept_no}`;
    try {
      const { status, raws } = await scanFiling(f, client, matcher, o);
      for (const r of raws) appendJsonl(rawPath, r);
      appendJsonl(scannedPath, status);
      log(`${tag} → ${status.status === "ok" ? `후보 ${raws.length}` : `❌ ${status.error}`}`);
    } catch (e) {
      if (e instanceof FatalDartError) {
        log(`⛔ ${e.message} — 중단. 같은 명령을 다시 실행하면 이어서 처리합니다.`);
        finalize(o, filings, log);
        process.exit(2);
      }
      const msg = e instanceof DartDocumentError ? e.message : `${(e as Error).message}`;
      appendJsonl(scannedPath, { rcept_no: f.rcept_no, status: "error", error: msg, at: new Date().toISOString() } satisfies ScanStatus);
      log(`${tag} → ❌ ${msg}`);
    }
  }
  finalize(o, filings, log);
}

if (require.main === module) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
