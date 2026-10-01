import fs from "fs";
import path from "path";
import axios from "axios";
import { DART_API_BASE } from "../../../src/services/opendart/constants";

/**
 * OpenDART list.json 항목. 공식 문서 필드명 그대로이며, 응답에 빠지는 필드가 있어도
 * 죽지 않도록 전부 문자열로 정규화해서 쓴다 (normalizeDisclosure).
 *  - rm: 비고. "유"(유가)·"코"(코스닥)·"채"·"넥"·"공"(공정위)·"연"(연결)·"정"(이후 정정 있음) 등의 조합
 */
export interface DartDisclosure {
  corp_code: string;
  corp_name: string;
  stock_code: string;
  corp_cls: string;
  report_nm: string;
  rcept_no: string;
  flr_nm: string;
  rcept_dt: string;
  rm: string;
}

export interface ListParams {
  corp_code?: string;
  bgn_de: string;
  end_de: string;
  pblntf_ty?: string;
  pblntf_detail_ty?: string;
  corp_cls?: string;
  last_reprt_at?: "Y" | "N";
}

/** 키/IP/일일 한도 문제 — 계속 호출해도 실패하므로 batch 전체를 멈추고 상태를 저장해야 하는 오류 */
export class FatalDartError extends Error {}
/** 해당 공시만 실패 (재시도해도 안 되는 문서 오류 등) — 기록하고 다음 공시로 */
export class DartDocumentError extends Error {}

const FATAL_STATUS = new Set(["010", "011", "012", "020", "101", "901"]);
// 일시 오류로 보고 재시도하는 상태 (800 시스템 점검, 900 정의되지 않은 오류)
const RETRY_STATUS = new Set(["800", "900"]);

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function normalizeDisclosure(raw: Record<string, unknown>): DartDisclosure {
  const s = (k: string) => (raw[k] == null ? "" : String(raw[k]).trim());
  return {
    corp_code: s("corp_code"),
    corp_name: s("corp_name"),
    stock_code: s("stock_code"),
    corp_cls: s("corp_cls"),
    report_nm: s("report_nm").replace(/\s+/g, " "),
    rcept_no: s("rcept_no"),
    flr_nm: s("flr_nm"),
    rcept_dt: s("rcept_dt"),
    rm: s("rm"),
  };
}

export function dartViewerUrl(rceptNo: string): string {
  return `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${rceptNo}`;
}

/** 회사 미지정 검색은 기간 3개월 제한 → [bgn, end]를 3개월 단위로 분할 (YYYYMMDD) */
export function splitPeriod(bgn: string, end: string, months = 3): Array<[string, string]> {
  const toDate = (s: string) => new Date(Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8)));
  const fmt = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, "");
  const out: Array<[string, string]> = [];
  let cur = toDate(bgn);
  const last = toDate(end);
  while (cur <= last) {
    const next = new Date(Date.UTC(cur.getUTCFullYear(), cur.getUTCMonth() + months, cur.getUTCDate()));
    const segEnd = new Date(Math.min(next.getTime() - 86400000, last.getTime()));
    out.push([fmt(cur), fmt(segEnd)]);
    cur = next;
  }
  return out;
}

export interface DartClientOptions {
  /** 호출 간 최소 간격 (DART는 분당 과다 호출 시 차단) */
  minIntervalMs?: number;
  /** document.xml ZIP 디스크 캐시 디렉터리 (재실행 시 재다운로드 방지) */
  cacheDir?: string;
  log?: (msg: string) => void;
}

export class DartClient {
  private lastCall = 0;
  private readonly minIntervalMs: number;
  private readonly log: (msg: string) => void;

  constructor(private readonly key: string, private readonly opts: DartClientOptions = {}) {
    this.minIntervalMs = opts.minIntervalMs ?? 400;
    this.log = opts.log ?? (() => {});
    if (opts.cacheDir) fs.mkdirSync(opts.cacheDir, { recursive: true });
  }

  private async throttle() {
    const wait = this.lastCall + this.minIntervalMs - Date.now();
    if (wait > 0) await sleep(wait);
    this.lastCall = Date.now();
  }

  /** 네트워크/5xx/일시 상태코드는 지수 백오프 재시도. Fatal·문서 오류는 즉시 전파 */
  private async withRetry<T>(label: string, fn: () => Promise<T>, attempts = 4): Promise<T> {
    let lastErr: unknown;
    for (let i = 0; i < attempts; i++) {
      try {
        await this.throttle();
        return await fn();
      } catch (e) {
        if (e instanceof FatalDartError || e instanceof DartDocumentError) throw e;
        lastErr = e;
        const status = axios.isAxiosError(e) ? e.response?.status : undefined;
        if (status && status >= 400 && status < 500 && status !== 429) throw e; // 요청 자체 오류
        if (i < attempts - 1) {
          const delay = [2000, 5000, 15000][i] ?? 15000;
          this.log(`⚠️ ${label} 재시도 ${i + 1}/${attempts - 1} (${(e as Error).message}) — ${delay / 1000}s 후`);
          await sleep(delay);
        }
      }
    }
    throw lastErr;
  }

  /** list.json 전체 페이지 수집 */
  async list(params: ListParams): Promise<DartDisclosure[]> {
    const out: DartDisclosure[] = [];
    for (let page = 1; ; page++) {
      const data = await this.withRetry(`list.json p${page}`, async () => {
        const res = await axios.get(`${DART_API_BASE}/list.json`, {
          params: { crtfc_key: this.key, page_no: page, page_count: 100, ...params },
          timeout: 20000,
        });
        const d = res.data ?? {};
        const status = String(d.status ?? "");
        if (FATAL_STATUS.has(status)) throw new FatalDartError(`OpenDART ${status}: ${d.message}`);
        if (RETRY_STATUS.has(status)) throw new Error(`OpenDART ${status}: ${d.message}`);
        return d;
      });
      const status = String(data.status ?? "");
      if (status === "013") break; // 조회된 데이터 없음
      if (status !== "000") throw new Error(`list.json 오류 ${status}: ${data.message}`);
      for (const raw of (data.list ?? []) as Record<string, unknown>[]) out.push(normalizeDisclosure(raw));
      if (page >= Number(data.total_page ?? 1)) break;
    }
    return out;
  }

  /** 캐시된 ZIP이 깨져 있으면 지워서 다음 실행 때 다시 받게 한다 */
  evictCache(rceptNo: string): void {
    if (this.opts.cacheDir) fs.rmSync(path.join(this.opts.cacheDir, `${rceptNo}.zip`), { force: true });
  }

  /**
   * document.xml → 공시서류 원본 ZIP. Content-Type에 의존하지 않고 ZIP 시그니처(PK)로 판별.
   * ZIP이 아니면 DART 오류 XML(<status>/<message>)로 보고 상태코드별로 처리.
   */
  async document(rceptNo: string): Promise<Buffer> {
    const cachePath = this.opts.cacheDir ? path.join(this.opts.cacheDir, `${rceptNo}.zip`) : null;
    if (cachePath && fs.existsSync(cachePath)) return fs.readFileSync(cachePath);

    const buf = await this.withRetry(`document.xml ${rceptNo}`, async () => {
      const res = await axios.get(`${DART_API_BASE}/document.xml`, {
        params: { crtfc_key: this.key, rcept_no: rceptNo },
        responseType: "arraybuffer",
        timeout: 90000,
      });
      const b = Buffer.from(res.data);
      if (b.subarray(0, 2).toString("latin1") === "PK") return b;
      const text = b.toString("utf8");
      const status = text.match(/<status>\s*(\d+)\s*<\/status>/i)?.[1] ?? "";
      const message = text.match(/<message>([^<]*)<\/message>/i)?.[1]?.trim() ?? text.replace(/\s+/g, " ").slice(0, 200);
      if (FATAL_STATUS.has(status)) throw new FatalDartError(`OpenDART ${status}: ${message}`);
      if (RETRY_STATUS.has(status) || !status) throw new Error(`document.xml 비정상 응답 ${status}: ${message}`);
      throw new DartDocumentError(`document.xml ${status}: ${message}`); // 014 파일 없음 등
    });
    if (cachePath) fs.writeFileSync(cachePath, buf);
    return buf;
  }
}
