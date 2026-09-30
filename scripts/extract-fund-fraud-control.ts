/**
 * 회사 리스트 → OpenDART 사업보고서 첨부 "내부회계관리제도 운영실태보고서"에서
 * 자금부정통제(횡령 등 자금 부정 예방·적발 통제활동) 공시 내역을 추출.
 *
 * 배경:
 *  - 자금부정통제 공시는 운영실태보고서(사업보고서/감사보고서 첨부서류)에 기재된다.
 *    감사(위원회) 평가보고서의 "자금 관련 부정위험에 대한 감사인과의 의사소통"도 함께 잡힌다.
 *  - FY2023~2024는 자율 기재, FY2025 사업보고서(2026.3 제출)부터 자산 1천억 이상 상장사 의무.
 *  - OpenDART document.xml ZIP에는 본문 + 첨부서류 XML이 함께 들어있으므로 전 파트를 검색한다.
 *
 * 사용법:
 *   npx tsx scripts/extract-fund-fraud-control.ts 005930 000660 카카오
 *   npx tsx scripts/extract-fund-fraud-control.ts --input companies.txt --from 2023 --to 2025
 *   npx tsx scripts/extract-fund-fraud-control.ts --input companies.txt --out ./fund-fraud-out
 *
 *   회사 식별자: 종목코드(6자리) / DART corp_code(8자리) / 회사명(정확 일치 우선) 모두 가능.
 *   --input 파일은 한 줄에 하나 (쉼표/탭 구분 시 첫 칸만 사용, # 주석 허용).
 *   --from/--to: 사업연도(회계연도) 범위. 기본값 2023 ~ (올해-1).
 *
 * 출력 (--out, 기본 ./fund-fraud-control-output):
 *   result.json  — 회사·연도별 추출 원문 전체
 *   summary.csv  — 회사·연도별 공시 여부 요약 (엑셀용 BOM 포함)
 *   result.md    — 사람이 읽기 좋은 마크다운 리포트
 *
 * 환경변수: OPENDART_API_KEY (.env.local / .env 자동 로드)
 */
import fs from "fs";
import path from "path";
import axios from "axios";
import AdmZip from "adm-zip";
import { DART_API_BASE } from "../src/services/opendart/constants";

// 환경변수 로드
for (const envFile of [".env.local", ".env"]) {
  const envPath = path.join(process.cwd(), envFile);
  if (fs.existsSync(envPath)) {
    const envContent = fs.readFileSync(envPath, "utf-8");
    for (const line of envContent.split("\n")) {
      const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*?)\s*$/);
      if (match && !process.env[match[1]]) process.env[match[1]] = match[2].replace(/^["']|["']$/g, "");
    }
    break;
  }
}

const DELAY_MS = 700; // DART 분당 호출 제한 회피
const MAX_SECTION_CHARS = 30000;

// ─────────────────────────────────────────────────────────────
// CLI 파싱
// ─────────────────────────────────────────────────────────────
interface CliOptions {
  companies: string[];
  fromYear: number;
  toYear: number;
  outDir: string;
}

function parseArgs(argv: string[]): CliOptions {
  const companies: string[] = [];
  const thisYear = new Date().getFullYear();
  let fromYear = 2023;
  let toYear = thisYear - 1;
  let outDir = path.resolve(process.cwd(), "fund-fraud-control-output");

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--input") {
      const file = argv[++i];
      for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
        const v = line.replace(/#.*/, "").split(/[,\t]/)[0].trim();
        if (v) companies.push(v);
      }
    } else if (a === "--from") fromYear = parseInt(argv[++i], 10);
    else if (a === "--to") toYear = parseInt(argv[++i], 10);
    else if (a === "--out") outDir = path.resolve(argv[++i]);
    else companies.push(a);
  }
  if (companies.length === 0) {
    console.error("사용법: npx tsx scripts/extract-fund-fraud-control.ts [--input file] [--from 2023] [--to 2025] [--out dir] <회사...>");
    process.exit(1);
  }
  return { companies, fromYear, toYear, outDir };
}

// ─────────────────────────────────────────────────────────────
// 회사 식별자 → corp_code
// ─────────────────────────────────────────────────────────────
interface CorpCodeEntry {
  corp_code: string;
  corp_name: string;
  stock_code: string;
}

function loadCorpCodes(): CorpCodeEntry[] {
  return JSON.parse(fs.readFileSync(path.resolve(__dirname, "../data/corp-codes.json"), "utf8"));
}

function resolveCompany(query: string, entries: CorpCodeEntry[]): CorpCodeEntry | null {
  const q = query.trim();
  if (/^\d{8}$/.test(q)) return entries.find((e) => e.corp_code === q) ?? null;
  if (/^[0-9A-Z]{6}$/.test(q)) return entries.find((e) => e.stock_code && e.stock_code.padStart(6, "0") === q) ?? null;
  // 회사명: 상장사 정확 일치 > 정확 일치 > 상장사 부분 일치
  const norm = (s: string) => s.replace(/\s|\(주\)|㈜|주식회사/g, "").toLowerCase();
  const nq = norm(q);
  const exact = entries.filter((e) => norm(e.corp_name) === nq);
  return (
    exact.find((e) => e.stock_code) ??
    exact[0] ??
    entries.find((e) => e.stock_code && norm(e.corp_name).includes(nq)) ??
    null
  );
}

// ─────────────────────────────────────────────────────────────
// DART API
// ─────────────────────────────────────────────────────────────
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function withRetry<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      if (i < attempts - 1) await sleep([1000, 3000, 8000][i] ?? 5000);
    }
  }
  throw lastErr;
}

interface DartListDoc {
  rcept_no: string;
  report_nm: string;
  rcept_dt: string;
}

/** 사업보고서(A001) 목록. 사업연도 fromYear~toYear 보고서는 이듬해에 제출되므로 제출일 범위를 +1년. */
async function listAnnualReports(corpCode: string, fromYear: number, toYear: number, key: string): Promise<DartListDoc[]> {
  const out: DartListDoc[] = [];
  let page = 1;
  while (true) {
    const res = await withRetry(() =>
      axios.get(`${DART_API_BASE}/list.json`, {
        params: {
          crtfc_key: key,
          corp_code: corpCode,
          bgn_de: `${fromYear}0101`,
          end_de: `${toYear + 1}1231`,
          pblntf_detail_ty: "A001",
          page_no: page,
          page_count: 100,
        },
        timeout: 15000,
      })
    );
    const d = res.data;
    if (d.status === "013") break; // 조회 데이터 없음
    if (d.status !== "000") throw new Error(`list.json 오류 ${d.status}: ${d.message}`);
    out.push(...(d.list as DartListDoc[]));
    if (page >= Number(d.total_page ?? 1)) break;
    page++;
    await sleep(DELAY_MS);
  }
  return out;
}

async function downloadDocumentZip(rceptNo: string, key: string): Promise<AdmZip> {
  const res = await withRetry(() =>
    axios.get(`${DART_API_BASE}/document.xml`, {
      params: { crtfc_key: key, rcept_no: rceptNo },
      responseType: "arraybuffer",
      timeout: 60000,
    })
  );
  const buf = Buffer.from(res.data);
  // 오류 시 ZIP이 아니라 XML/JSON 에러 메시지가 온다
  if (buf.length < 1000 || buf.subarray(0, 2).toString() !== "PK") {
    throw new Error(`document.xml 다운로드 실패: ${buf.toString("utf8").slice(0, 200)}`);
  }
  return new AdmZip(buf);
}

// ─────────────────────────────────────────────────────────────
// 자금부정통제 섹션 추출
// ─────────────────────────────────────────────────────────────

/** "사업보고서 (2024.12)" → "2024.12" (결산기). 없으면 null */
function fiscalPeriodOf(reportNm: string): string | null {
  return reportNm.match(/\((\d{4}\.\d{2})\)/)?.[1] ?? null;
}

const isAmend = (nm: string) => /\[.*정정.*\]/.test(nm);

/** XML → 평문 (표는 | 구분 행으로 보존) */
export function xmlToText(xml: string): string {
  return xml
    .replace(/<(TD|TH|TE|TU)[^>]*>/gi, " | ")
    .replace(/<\/(TR)>/gi, " |\n")
    .replace(/<\/(P|DIV|BR|LI|TITLE|H[1-6])>/gi, "\n")
    .replace(/<BR\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;|&#160;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/[ \t]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function documentName(xml: string): string {
  const m = xml.match(/<DOCUMENT-NAME[^>]*>([^<]+)</i);
  return m ? m[1].trim() : "";
}

// 자금부정통제 관련 제목/키워드
const HEADING_RE =
  /(자\s*금\s*(관\s*련\s*)?부\s*정|횡\s*령\s*등\s*자\s*금|자\s*금\s*사\s*고)[^\n]{0,60}(통\s*제|예\s*방|적\s*발|위\s*험|의\s*사\s*소\s*통|점\s*검)/;
const KEYWORD_RE = /자\s*금\s*(관\s*련\s*)?부\s*정|횡\s*령\s*등\s*자\s*금/g;
// 다음 상위 제목: 로마숫자 / "Ⅳ." / "4." / "가." 류 시작 행 (단, 자금부정 관련 제목은 제외)
const TOP_HEADING_RE = /^\s*(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|(?:X|IX|IV|V?I{1,3}|V))\s*[.．]\s*\S/;

export interface ExtractedSection {
  heading: string;
  text: string;
}

/**
 * 평문에서 자금부정통제 섹션 후보를 찾아 반환.
 * - 제목 행(짧은 행 + HEADING_RE)에서 시작 → 다음 로마숫자 상위 제목 전까지.
 * - 목차는 짧고 본문은 길기 때문에, 같은 제목이 여러 번 나오면 긴 쪽만 남긴다.
 */
export function extractFundFraudSections(text: string): ExtractedSection[] {
  const lines = text.split("\n");
  const found: ExtractedSection[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const isHeading = line.length <= 120 && !line.includes("|") && HEADING_RE.test(line);
    if (!isHeading) {
      i++;
      continue;
    }
    let j = i + 1;
    let len = 0;
    for (; j < lines.length; j++) {
      const l = lines[j];
      // 다음 상위 제목(로마숫자)에서 종료. 하위 번호(1. 가. 등)는 섹션 내부로 보고 계속 포함
      // (덜 자르는 쪽이 안전 — 과다 수집은 MAX_SECTION_CHARS로 제한)
      if (TOP_HEADING_RE.test(l) && !HEADING_RE.test(l)) break;
      len += l.length + 1;
      if (len > MAX_SECTION_CHARS) break;
    }
    const body = lines.slice(i, j).join("\n").trim();
    found.push({ heading: line.trim(), text: body });
    i = j;
  }
  // 목차성(짧은) 후보 제거: 동일 제목 중 가장 긴 것만, 그리고 본문 100자 미만 제거
  const byHeading = new Map<string, ExtractedSection>();
  for (const s of found) {
    const k = s.heading.replace(/\s/g, "");
    const prev = byHeading.get(k);
    if (!prev || s.text.length > prev.text.length) byHeading.set(k, s);
  }
  return [...byHeading.values()].filter((s) => s.text.length - s.heading.length >= 100);
}

/** 제목을 못 찾았을 때 대비: 키워드 주변 문맥 스니펫 */
function keywordSnippets(text: string, max = 5): string[] {
  const out: string[] = [];
  KEYWORD_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  let lastEnd = -1;
  while ((m = KEYWORD_RE.exec(text)) !== null && out.length < max) {
    if (m.index < lastEnd) continue;
    const s = Math.max(0, m.index - 200);
    const e = Math.min(text.length, m.index + 600);
    out.push(text.slice(s, e).replace(/\n+/g, " ").trim());
    lastEnd = e;
  }
  return out;
}

interface PartResult {
  entry: string;
  documentName: string;
  sections: ExtractedSection[];
  snippets: string[];
}

function extractFromZip(zip: AdmZip): PartResult[] {
  const results: PartResult[] = [];
  for (const entry of zip.getEntries()) {
    if (!entry.entryName.toLowerCase().endsWith(".xml")) continue;
    let xml: string;
    try {
      xml = entry.getData().toString("utf8");
    } catch {
      continue;
    }
    const text = xmlToText(xml);
    KEYWORD_RE.lastIndex = 0;
    if (!KEYWORD_RE.test(text)) continue;
    const sections = extractFundFraudSections(text);
    results.push({
      entry: entry.entryName,
      documentName: documentName(xml),
      sections,
      snippets: sections.length === 0 ? keywordSnippets(text) : [],
    });
  }
  // 운영실태보고서 → 평가보고서 → 기타(본문/감사보고서) 순
  const rank = (p: PartResult) =>
    /운영\s*실태/.test(p.documentName) ? 0 : /평가\s*보고/.test(p.documentName) ? 1 : 2;
  return results.sort((a, b) => rank(a) - rank(b));
}

// ─────────────────────────────────────────────────────────────
// 메인
// ─────────────────────────────────────────────────────────────
type Status = "disclosed" | "keyword_only" | "not_found" | "no_report" | "error";

interface YearResult {
  fiscalPeriod: string;
  rceptNo: string;
  reportName: string;
  rceptDate: string;
  dartUrl: string;
  status: Status;
  parts: PartResult[];
  error?: string;
}

interface CompanyResult {
  query: string;
  corpCode: string | null;
  corpName: string | null;
  stockCode: string | null;
  years: YearResult[];
  error?: string;
}

async function processCompany(query: string, corp: CorpCodeEntry, opts: CliOptions, key: string): Promise<CompanyResult> {
  const result: CompanyResult = {
    query,
    corpCode: corp.corp_code,
    corpName: corp.corp_name,
    stockCode: corp.stock_code || null,
    years: [],
  };

  const docs = await listAnnualReports(corp.corp_code, opts.fromYear, opts.toYear, key);
  await sleep(DELAY_MS);

  // 결산기별 그룹: 최신 제출(정정 포함) 우선 시도, 섹션 없으면 이전 제출로 fallback
  const byPeriod = new Map<string, DartListDoc[]>();
  for (const d of docs) {
    const p = fiscalPeriodOf(d.report_nm);
    if (!p) continue;
    const y = parseInt(p.slice(0, 4), 10);
    if (y < opts.fromYear || y > opts.toYear) continue;
    if (!byPeriod.has(p)) byPeriod.set(p, []);
    byPeriod.get(p)!.push(d);
  }

  for (let y = opts.fromYear; y <= opts.toYear; y++) {
    const periods = [...byPeriod.keys()].filter((p) => p.startsWith(`${y}.`)).sort();
    if (periods.length === 0) {
      result.years.push({
        fiscalPeriod: String(y),
        rceptNo: "",
        reportName: "",
        rceptDate: "",
        dartUrl: "",
        status: "no_report",
        parts: [],
      });
      continue;
    }
    for (const period of periods) {
      const candidates = byPeriod
        .get(period)!
        .sort((a, b) => b.rcept_dt.localeCompare(a.rcept_dt) || Number(isAmend(a.report_nm)) - Number(isAmend(b.report_nm)));

      let best: YearResult | null = null;
      for (const doc of candidates) {
        const yr: YearResult = {
          fiscalPeriod: period,
          rceptNo: doc.rcept_no,
          reportName: doc.report_nm.trim(),
          rceptDate: doc.rcept_dt,
          dartUrl: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${doc.rcept_no}`,
          status: "not_found",
          parts: [],
        };
        try {
          const zip = await downloadDocumentZip(doc.rcept_no, key);
          yr.parts = extractFromZip(zip);
          yr.status = yr.parts.some((p) => p.sections.length > 0)
            ? "disclosed"
            : yr.parts.length > 0
              ? "keyword_only"
              : "not_found";
        } catch (e: any) {
          yr.status = "error";
          yr.error = e.message;
        }
        await sleep(DELAY_MS);
        if (!best || rankStatus(yr.status) < rankStatus(best.status)) best = yr;
        if (best.status === "disclosed") break;
      }
      result.years.push(best!);
    }
  }
  return result;
}

function rankStatus(s: Status): number {
  return { disclosed: 0, keyword_only: 1, not_found: 2, error: 3, no_report: 4 }[s];
}

function csvCell(v: string): string {
  return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
}

function writeOutputs(results: CompanyResult[], outDir: string) {
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(results, null, 2));

  const header = ["입력값", "회사명", "종목코드", "corp_code", "결산기", "상태", "보고서명", "접수일", "접수번호", "추출섹션", "추출글자수", "DART링크", "오류"];
  const rows = [header.join(",")];
  for (const c of results) {
    if (c.years.length === 0) {
      rows.push([c.query, c.corpName ?? "", c.stockCode ?? "", c.corpCode ?? "", "", "error", "", "", "", "", "", "", c.error ?? ""].map(csvCell).join(","));
    }
    for (const y of c.years) {
      const sections = y.parts.flatMap((p) => p.sections);
      rows.push(
        [
          c.query,
          c.corpName ?? "",
          c.stockCode ?? "",
          c.corpCode ?? "",
          y.fiscalPeriod,
          y.status,
          y.reportName,
          y.rceptDate,
          y.rceptNo,
          sections.map((s) => s.heading).join(" / "),
          String(sections.reduce((n, s) => n + s.text.length, 0)),
          y.dartUrl,
          y.error ?? "",
        ]
          .map(csvCell)
          .join(",")
      );
    }
  }
  fs.writeFileSync(path.join(outDir, "summary.csv"), "﻿" + rows.join("\n"));

  const md: string[] = ["# 자금부정통제 공시 추출 결과", ""];
  for (const c of results) {
    md.push(`## ${c.corpName ?? c.query} (${c.stockCode ?? "-"} / ${c.corpCode ?? "-"})`, "");
    if (c.error) md.push(`> ❌ ${c.error}`, "");
    for (const y of c.years) {
      md.push(`### ${y.fiscalPeriod} — ${y.status}`, "");
      if (y.rceptNo) md.push(`- 보고서: [${y.reportName}](${y.dartUrl}) (접수 ${y.rceptDate})`);
      if (y.error) md.push(`- 오류: ${y.error}`);
      md.push("");
      for (const p of y.parts) {
        if (p.sections.length === 0 && p.snippets.length === 0) continue;
        md.push(`#### 📎 ${p.documentName || p.entry}`, "");
        for (const s of p.sections) md.push("```text", s.text, "```", "");
        for (const sn of p.snippets) md.push(`> …${sn}…`, "");
      }
    }
  }
  fs.writeFileSync(path.join(outDir, "result.md"), md.join("\n"));
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const key = process.env.OPENDART_API_KEY;
  if (!key) {
    console.error("❌ OPENDART_API_KEY가 설정되지 않았습니다 (.env.local 또는 환경변수).");
    process.exit(1);
  }
  const corpCodes = loadCorpCodes();
  console.log(`🔎 ${opts.companies.length}개 회사, 사업연도 ${opts.fromYear}~${opts.toYear}`);

  const results: CompanyResult[] = [];
  for (const [idx, q] of opts.companies.entries()) {
    const corp = resolveCompany(q, corpCodes);
    const tag = `[${idx + 1}/${opts.companies.length}] ${q}`;
    if (!corp) {
      console.warn(`${tag} ❌ corp_code를 찾을 수 없음`);
      results.push({ query: q, corpCode: null, corpName: null, stockCode: null, years: [], error: "corp_code 매핑 실패" });
      continue;
    }
    try {
      const r = await processCompany(q, corp, opts, key);
      results.push(r);
      console.log(`${tag} ${corp.corp_name}: ${r.years.map((y) => `${y.fiscalPeriod}=${y.status}`).join(", ")}`);
    } catch (e: any) {
      console.warn(`${tag} ❌ ${e.message}`);
      results.push({ query: q, corpCode: corp.corp_code, corpName: corp.corp_name, stockCode: corp.stock_code || null, years: [], error: e.message });
    }
    // 중간 저장 (대량 리스트 중단 대비)
    writeOutputs(results, opts.outDir);
  }
  console.log(`✅ 완료 → ${opts.outDir}/{result.json, summary.csv, result.md}`);
}

if (require.main === module) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
