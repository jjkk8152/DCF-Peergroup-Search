import type { DartDisclosure } from "./dart";

/**
 * 정정공시 관계.
 *  - 보고서명 앞 대괄호 태그([기재정정], [첨부정정], [첨부추가], [변경등록] …)가 정정/추가/변경이면 정정공시로 본다.
 *  - 같은 회사·같은 기본 보고서명(태그 제거) 안에서, 정정공시는 바로 앞 공시를 정정한 것으로 연결.
 *    (사업보고서는 보고서명에 "(2024.12)"가 들어 있어 결산기별로 자연히 분리된다)
 *  - list.json의 rm에 "정"이 있으면 "이후 정정 공시 있음" — 조회기간 밖 정정까지 반영되는 공식 플래그.
 */
export interface AmendmentInfo {
  is_amendment: boolean;
  /** 이 공시가 정정한 직전 공시 (조회 범위 안에서 찾은 경우) */
  amends_rcept_no: string | null;
  /** 정정 체인의 최초 원공시 */
  original_rcept_no: string;
  /** 같은 체인에서 가장 최근 제출본인가 */
  is_latest: boolean;
  /** rm "정" — 이 공시 이후 정정 공시가 있음 (DART 공식 플래그) */
  has_later_amendment: boolean;
}

const TAG_RE = /^\s*(\[[^\]]*\]\s*)+/;

export function isAmendmentName(reportNm: string): boolean {
  const tags = reportNm.match(TAG_RE)?.[0] ?? "";
  return /정정|추가|변경/.test(tags);
}

export function baseReportName(reportNm: string): string {
  return reportNm.replace(TAG_RE, "").replace(/\s+/g, "");
}

export function linkAmendments(filings: DartDisclosure[]): Map<string, AmendmentInfo> {
  const groups = new Map<string, DartDisclosure[]>();
  for (const f of filings) {
    const key = `${f.corp_code}|${baseReportName(f.report_nm)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(f);
  }
  const info = new Map<string, AmendmentInfo>();
  for (const list of groups.values()) {
    list.sort((a, b) => a.rcept_no.localeCompare(b.rcept_no));
    for (let i = 0; i < list.length; i++) {
      const f = list[i];
      const amend = isAmendmentName(f.report_nm);
      const prev = amend && i > 0 ? list[i - 1] : null;
      info.set(f.rcept_no, {
        is_amendment: amend,
        amends_rcept_no: prev?.rcept_no ?? null,
        original_rcept_no: prev ? info.get(prev.rcept_no)!.original_rcept_no : f.rcept_no,
        is_latest: true,
        has_later_amendment: f.rm.includes("정"),
      });
      if (prev) info.get(prev.rcept_no)!.is_latest = false;
    }
  }
  for (const f of filings) {
    const i = info.get(f.rcept_no)!;
    if (i.has_later_amendment) i.is_latest = false; // 범위 밖 정정이 있어도 최신본 아님
  }
  return info;
}
