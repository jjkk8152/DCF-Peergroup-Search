import AdmZip from "adm-zip";

/**
 * 공시서류 원본 ZIP → 문서별 chunk 목록.
 *
 * DART 원본 ZIP 안에는 본문 XML(dart4.xsd 기반: SECTION-n / TITLE / P / TABLE·TR·TD·TE·TU)과
 * 첨부서류 XML(감사보고서, 내부회계 운영실태보고서 등)이 파일별로 들어있고, 오래된 공시는 HTML도 있다.
 * 태그 이름에 의존하는 부분은 TITLE/TR/셀 정도로 최소화하고 나머지는 블록 태그 → 줄바꿈으로 일반화했다.
 */

export type ChunkType = "heading" | "paragraph" | "table_row";

export interface Chunk {
  idx: number;
  type: ChunkType;
  text: string;
  /** 직전 제목들의 경로 ("III. 재무에 관한 사항 > 5. 재무제표 주석") */
  section: string;
}

export interface ParsedDocument {
  /** ZIP 내 파일명 */
  entry: string;
  /** <DOCUMENT-NAME> 또는 <title>, 없으면 파일명 */
  documentName: string;
  encoding: string;
  chunks: Chunk[];
}

export interface ParsedZip {
  documents: ParsedDocument[];
  /** 파일 단위 오류 (깨진 엔트리, 디코딩 실패 등) — 전체를 중단시키지 않는다 */
  errors: string[];
}

const DOC_EXT_RE = /\.(xml|html?|txt)$/i;
const MAX_PARAGRAPH = 1500;

/** 바이트 → 문자열. BOM → 선언된 인코딩(XML decl / meta charset) → UTF-8 엄격 → CP949 순 */
export function decodeBytes(buf: Buffer): { text: string; encoding: string } {
  if (buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) return { text: buf.subarray(3).toString("utf8"), encoding: "utf-8-bom" };
  if (buf[0] === 0xff && buf[1] === 0xfe) return { text: new TextDecoder("utf-16le").decode(buf), encoding: "utf-16le" };
  const head = buf.subarray(0, 1024).toString("latin1");
  const declared = (head.match(/encoding\s*=\s*["']([\w-]+)["']/i) ?? head.match(/charset\s*=\s*["']?([\w-]+)/i))?.[1]?.toLowerCase();
  if (declared && /euc-?kr|cp949|ks_c_5601|ms949/.test(declared)) {
    return { text: new TextDecoder("euc-kr").decode(buf), encoding: "euc-kr" };
  }
  try {
    return { text: new TextDecoder("utf-8", { fatal: true }).decode(buf), encoding: "utf-8" };
  } catch {
    return { text: new TextDecoder("euc-kr").decode(buf), encoding: "euc-kr(추정)" };
  }
}

function decodeEntities(s: string): string {
  return s
    .replace(/&nbsp;|&#160;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&amp;/gi, "&");
}

/** 제목 번호 체계 레벨: 1=Ⅰ./I., 2=1., 3=가., 4=(1)·1), 5=(가)·가), 0=번호 없음 */
export function headingLevel(line: string): number {
  const l = line.trim();
  if (/^(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|X|IX|IV|V?I{1,3}|V)\s*[.．]\s*\S/.test(l)) return 1;
  if (/^\d{1,2}\s*[.．]\s*[^\d\s]/.test(l)) return 2; // "2025.12" 같은 숫자는 제외
  if (/^[가나다라마바사아자차카타파하]\s*[.．]\s*\S/.test(l)) return 3;
  if (/^(?:\(\s*\d{1,2}\s*\)|\d{1,2}\s*\))\s*\S/.test(l)) return 4;
  if (/^(?:\(\s*[가나다라마바사아자차카타파하]\s*\)|[가나다라마바사아자차카타파하]\s*\))\s*\S/.test(l)) return 5;
  return 0;
}

const SENTENCE_END_RE = /(다|요|음|함|임)\s*[.。]?\s*$/;

/** 긴 문단은 문장 경계("다. ")에서 ~MAX_PARAGRAPH 단위로 분할 (후보 문맥이 과도하게 커지지 않도록) */
function splitLong(text: string): string[] {
  if (text.length <= MAX_PARAGRAPH) return [text];
  const sentences = text.split(/(?<=[다요음함]\.)\s+/);
  const out: string[] = [];
  let buf = "";
  for (const s of sentences) {
    if (buf && buf.length + s.length > MAX_PARAGRAPH) {
      out.push(buf);
      buf = "";
    }
    buf = buf ? `${buf} ${s}` : s;
    while (buf.length > MAX_PARAGRAPH * 2) {
      out.push(buf.slice(0, MAX_PARAGRAPH));
      buf = buf.slice(MAX_PARAGRAPH);
    }
  }
  if (buf) out.push(buf);
  return out;
}

const H = "\u0001H"; // 제목 마커
const R = "\u0001R"; // 표 행 마커
const C = "\u0002"; // 셀 구분

/** 마크업(XML/HTML/TXT) → chunk 배열 */
export function markupToChunks(markup: string): Chunk[] {
  const flat = markup
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<(DOCUMENT-NAME|TITLE|COVER-TITLE|H[1-6])\b[^>]*>/gi, `\n${H}`)
    .replace(/<\/(DOCUMENT-NAME|TITLE|COVER-TITLE|H[1-6])>/gi, "\n")
    .replace(/<TR\b[^>]*>/gi, `\n${R}`)
    .replace(/<\/TR>/gi, "\n")
    .replace(/<(TD|TH|TE|TU)\b[^>]*>/gi, C)
    .replace(/<\/?(P|DIV|BR|LI|UL|OL|TABLE|TBODY|THEAD|SECTION-\d|LIBRARY|BODY|PGBRK|CORRECTION|SPAN-BREAK)\b[^>]*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, " ");

  const chunks: Chunk[] = [];
  // 제목 스택: [레벨, 텍스트]. TITLE 태그는 레벨 0(최상위), 번호 제목은 headingLevel
  const stack: Array<[number, string]> = [];
  const section = () => stack.map((s) => s[1]).slice(-3).join(" > ");
  const push = (type: ChunkType, text: string) => {
    for (const t of type === "paragraph" ? splitLong(text) : [text]) chunks.push({ idx: chunks.length, type, text: t, section: section() });
  };
  const pushHeading = (level: number, text: string) => {
    while (stack.length && stack[stack.length - 1][0] >= level) stack.pop();
    stack.push([level, text.slice(0, 80)]);
    chunks.push({ idx: chunks.length, type: "heading", text, section: section() });
  };

  for (const rawLine of flat.split("\n")) {
    const isHeadingTag = rawLine.startsWith(H);
    const isRow = rawLine.startsWith(R);
    if (isRow) {
      const cells = rawLine
        .slice(R.length)
        .split(C)
        .map((c) => decodeEntities(c).replace(/\s+/g, " ").trim())
        .filter(Boolean);
      if (cells.length) push("table_row", cells.join(" | "));
      continue;
    }
    const body = isHeadingTag ? rawLine.slice(H.length) : rawLine;
    const text = decodeEntities(body.replace(/[\u0001\u0002]/g, " ")).replace(/\s+/g, " ").trim();
    if (!text) continue;
    if (isHeadingTag) {
      pushHeading(headingLevel(text), text); // 번호 없는 TITLE(문서 제목 등)은 레벨 0 = 최상위
    } else if (text.length <= 60 && headingLevel(text) > 0 && !SENTENCE_END_RE.test(text)) {
      pushHeading(headingLevel(text), text);
    } else {
      push("paragraph", text);
    }
  }
  return chunks;
}

export function documentNameOf(markup: string, entry: string): string {
  const m = markup.match(/<DOCUMENT-NAME[^>]*>([^<]+)</i) ?? markup.match(/<title[^>]*>([^<]+)</i);
  return m ? decodeEntities(m[1]).trim() : entry;
}

/** ZIP 버퍼 → 문서별 chunk. 깨진 ZIP은 throw, 개별 엔트리 오류는 errors에 기록 */
export function parseZip(buf: Buffer): ParsedZip {
  const zip = new AdmZip(buf); // 깨진 ZIP이면 여기서 throw → 호출부가 공시 단위 오류로 기록
  const documents: ParsedDocument[] = [];
  const errors: string[] = [];
  for (const entry of zip.getEntries()) {
    if (entry.isDirectory || !DOC_EXT_RE.test(entry.entryName)) continue;
    try {
      const { text, encoding } = decodeBytes(entry.getData());
      documents.push({ entry: entry.entryName, documentName: documentNameOf(text, entry.entryName), encoding, chunks: markupToChunks(text) });
    } catch (e) {
      errors.push(`${entry.entryName}: ${(e as Error).message}`);
    }
  }
  return { documents, errors };
}
