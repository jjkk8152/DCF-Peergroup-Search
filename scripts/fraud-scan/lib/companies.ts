import fs from "fs";
import path from "path";

export interface CorpCodeEntry {
  corp_code: string;
  corp_name: string;
  stock_code: string;
}

export function loadCorpCodes(): CorpCodeEntry[] {
  return JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../../data/corp-codes.json"), "utf8"));
}

/**
 * 입력 파일 인코딩 자동 판별. 윈도우 메모장(구버전 ANSI)·엑셀 CSV 기본 저장은 CP949라
 * UTF-8로만 읽으면 한글 회사명이 깨진다. UTF-8(BOM 포함) → UTF-16LE(BOM) → CP949 순.
 */
export function readTextAuto(file: string): string {
  const buf = fs.readFileSync(file);
  if (buf[0] === 0xff && buf[1] === 0xfe) return new TextDecoder("utf-16le").decode(buf);
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return new TextDecoder("euc-kr").decode(buf); // WHATWG euc-kr = windows-949
  }
}

/** 목록 파일 → 회사 식별자 배열 (한 줄 하나, # 주석, CSV/TSV는 첫 칸만) */
export function readCompanyList(file: string): string[] {
  const out: string[] = [];
  for (const line of readTextAuto(file).split(/\r?\n/)) {
    const v = line.replace(/#.*/, "").split(/[,\t]/)[0].replace(/^"|"$/g, "").trim();
    if (v) out.push(v);
  }
  return out;
}

/** 엑셀 CSV 첫 줄 헤더 판별용 */
export const HEADER_LIKE_RE = /회사|종목|코드|기업|name|code|corp/i;

/** 종목코드(6) / 고유번호(8) / 회사명 → corp-codes 항목 */
export function resolveCompany(query: string, entries: CorpCodeEntry[]): CorpCodeEntry | null {
  const q = query.trim();
  const byStock = (code: string) => entries.find((e) => e.stock_code && e.stock_code.padStart(6, "0") === code) ?? null;
  const byCorp = (code: string) => entries.find((e) => e.corp_code === code) ?? null;
  if (/^\d{8}$/.test(q)) return byCorp(q);
  if (/^[0-9A-Z]{6}$/.test(q)) return byStock(q);
  // 엑셀이 앞자리 0을 지운 경우 (005930 → 5930, 00126380 → 126380): 종목코드 → 고유번호 순으로 복원
  if (/^\d{1,7}$/.test(q)) return (q.length <= 6 ? byStock(q.padStart(6, "0")) : null) ?? byCorp(q.padStart(8, "0"));
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
