import fs from "fs";
import path from "path";
import crypto from "crypto";
import type { Chunk } from "./document";

/**
 * 1차 rule-based scan.
 * 단일 키워드 hit는 점수가 없다 — 같은 chunk(전점) 또는 ±window chunk(adjacent_factor 배)에서
 * fraud+cash / fraud+control / cash+control 조합이 나와야 점수가 붙고, 보너스 용어도 조합이 있을 때만 가산.
 *   "당사는 자금 집행에 대한 내부통제를 운영하고 있습니다." → cash+control = 2점 (기본 임계값 5 미만 → 탈락)
 */

type Category = "fraud" | "cash" | "control";
const CATEGORIES: Category[] = ["fraud", "cash", "control"];

export interface KeywordConfig {
  fraud: string[];
  cash: string[];
  control: string[];
  bonus: Record<string, number>;
  exclude: Record<string, string[]>;
  weights: { "fraud+cash": number; "fraud+control": number; "cash+control": number; adjacent_factor: number };
  category_hints: Record<string, { require_any: string[]; markers?: string[] }>;
  /** 감점: any 중 하나가 있고, 문맥 힌트에 unless_hints가 없을 때 points(음수) 가산 */
  penalties?: Array<{ name: string; any: string[]; points: number; unless_hints?: string[] }>;
}

export const DEFAULT_KEYWORDS_PATH = path.resolve(__dirname, "../keywords.json");

export function loadKeywords(file: string = DEFAULT_KEYWORDS_PATH): KeywordConfig {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** "중요한 취약점" → /중\s*요\s*한\s*취\s*약\s*점/ (공백 무시), exclude는 같은 위치 부정 전방탐색 */
function termRegex(term: string, excludes: string[] = []): RegExp {
  const flex = (t: string) => [...t.replace(/\s+/g, "")].map(escapeRe).join("\\s*");
  const neg = excludes.length ? `(?!${excludes.map(flex).join("|")})` : "";
  return new RegExp(neg + flex(term), "i");
}

export interface ChunkHits {
  fraud: string[];
  cash: string[];
  control: string[];
  bonus: string[];
}

export class KeywordMatcher {
  private readonly compiled: Record<Category, Array<[string, RegExp]>>;
  private readonly bonusRe: Array<[string, RegExp, number]>;
  private readonly hintRe: Array<[string, RegExp[], RegExp[]]>;
  readonly penalties: Array<{ name: string; res: RegExp[]; points: number; unless: string[] }>;

  constructor(readonly cfg: KeywordConfig) {
    const comp = (terms: string[]) => terms.map((t) => [t, termRegex(t, cfg.exclude[t])] as [string, RegExp]);
    this.compiled = { fraud: comp(cfg.fraud), cash: comp(cfg.cash), control: comp(cfg.control) };
    this.bonusRe = Object.entries(cfg.bonus).map(([t, w]) => [t, termRegex(t, cfg.exclude[t]), w]);
    this.hintRe = Object.entries(cfg.category_hints).map(([name, h]) => [
      name,
      h.require_any.map((t) => termRegex(t, cfg.exclude[t])),
      (h.markers ?? []).map((t) => termRegex(t)),
    ]);
    this.penalties = (cfg.penalties ?? []).map((p) => ({ name: p.name, res: p.any.map((t) => termRegex(t)), points: p.points, unless: p.unless_hints ?? [] }));
  }

  match(text: string): ChunkHits {
    const hits: ChunkHits = { fraud: [], cash: [], control: [], bonus: [] };
    for (const cat of CATEGORIES) for (const [t, re] of this.compiled[cat]) if (re.test(text)) hits[cat].push(t);
    for (const [t, re] of this.bonusRe) if (re.test(text)) hits.bonus.push(t);
    return hits;
  }

  bonusOf(term: string): number {
    return this.bonusRe.find(([t]) => t === term)?.[2] ?? 0;
  }

  /** A/B/C 규칙 힌트 (최종 판정은 LLM 분류 단계) */
  hints(text: string): string[] {
    return this.hintRe
      .filter(([, req, markers]) => req.some((r) => r.test(text)) && (markers.length === 0 || markers.some((m) => m.test(text))))
      .map(([name]) => name);
  }
}

export interface ScoredChunk {
  score: number;
  hits: ChunkHits;
  reasons: string[];
}

export function scoreChunks(chunks: Chunk[], matcher: KeywordMatcher, window = 2): ScoredChunk[] {
  const hits = chunks.map((c) => matcher.match(c.text));
  const w = matcher.cfg.weights;
  const pairs: Array<[Category, Category, number]> = [
    ["fraud", "cash", w["fraud+cash"]],
    ["fraud", "control", w["fraud+control"]],
    ["cash", "control", w["cash+control"]],
  ];
  return chunks.map((_, i) => {
    const own = hits[i];
    const near = (cat: Category) => {
      for (let j = Math.max(0, i - window); j <= Math.min(chunks.length - 1, i + window); j++) if (j !== i && hits[j][cat].length) return true;
      return false;
    };
    let score = 0;
    const reasons: string[] = [];
    for (const [a, b, pts] of pairs) {
      if (own[a].length && own[b].length) {
        score += pts;
        reasons.push(`${a}+${b}(+${pts})`);
      } else if ((own[a].length && near(b)) || (own[b].length && near(a))) {
        const p = pts * w.adjacent_factor;
        score += p;
        reasons.push(`${a}+${b}~인접(+${p})`);
      }
    }
    if (score > 0) {
      for (const t of own.bonus) {
        const b = matcher.bonusOf(t);
        score += b;
        reasons.push(`${t}(+${b})`);
      }
      if (matcher.penalties.length) {
        const winText = chunks.slice(Math.max(0, i - window), i + window + 1).map((c) => c.text).join("\n");
        const hints = matcher.hints(winText);
        for (const p of matcher.penalties) {
          if (p.res.some((r) => r.test(chunks[i].text)) && !p.unless.some((h) => hints.includes(h))) {
            score += p.points;
            reasons.push(`${p.name}(${p.points})`);
          }
        }
      }
    }
    return { score: Math.max(0, Math.round(score * 10) / 10), hits: own, reasons };
  });
}

export interface Candidate {
  /** evidence 정규화 hash — 문서/공시 간 중복 제거 키 */
  hash: string;
  score: number;
  section: string;
  /** 점수 임계값을 넘은 chunk 원문 (근거문장) */
  evidence: string;
  /** 앞뒤 chunk 포함 문맥 (▶ = 근거 chunk) */
  context: string;
  keywords: ChunkHits;
  reasons: string[];
  hints: string[];
  chunkRange: [number, number];
}

export interface ExtractOptions {
  minScore?: number;
  /** 근거 chunk 앞뒤로 붙일 chunk 수 */
  contextChunks?: number;
  maxContextChars?: number;
  /** 한 후보에 묶을 최대 근거 chunk 수 (긴 표 전체가 한 덩어리가 되는 것 방지) */
  maxAnchors?: number;
}

const uniq = <T>(a: T[]) => [...new Set(a)];

export function normalizeForHash(s: string): string {
  return s.replace(/[\s\p{P}\p{S}]/gu, "").toLowerCase();
}

export function extractCandidates(chunks: Chunk[], matcher: KeywordMatcher, opts: ExtractOptions = {}): Candidate[] {
  const { minScore = 5, contextChunks = 2, maxContextChars = 4000, maxAnchors = 8 } = opts;
  const scored = scoreChunks(chunks, matcher, 2);
  // 제목 chunk는 근거가 될 수 없다 (문맥에는 포함) — 제목만 걸려 근거문장이 비는 것 방지
  const anchors = scored.map((s, i) => (s.score >= minScore && chunks[i].type !== "heading" ? i : -1)).filter((i) => i >= 0);

  // 문맥 창이 겹치거나 맞닿는 근거 chunk는 한 후보로 병합
  const groups: number[][] = [];
  for (const a of anchors) {
    const g = groups[groups.length - 1];
    if (g && a - g[g.length - 1] <= contextChunks * 2 + 1 && g.length < maxAnchors) g.push(a);
    else groups.push([a]);
  }

  return groups.map((g) => {
    let ctx = contextChunks;
    const render = (n: number) => {
      const start = Math.max(0, g[0] - n);
      const end = Math.min(chunks.length - 1, g[g.length - 1] + n);
      const lines: string[] = [];
      for (let i = start; i <= end; i++) {
        const c = chunks[i];
        lines.push(`${g.includes(i) ? "▶ " : "  "}${c.type === "heading" ? "## " : c.type === "table_row" ? "| " : ""}${c.text}`);
      }
      return { text: lines.join("\n"), start, end };
    };
    let r = render(ctx);
    while (r.text.length > maxContextChars && ctx > 0) r = render(--ctx);
    if (r.text.length > maxContextChars) r = { ...r, text: r.text.slice(0, maxContextChars) + " …(생략)" };

    const evidence = g.map((i) => chunks[i].text).join("\n");
    const keywords: ChunkHits = { fraud: [], cash: [], control: [], bonus: [] };
    for (const i of g) for (const k of Object.keys(keywords) as (keyof ChunkHits)[]) keywords[k].push(...scored[i].hits[k]);
    for (const k of Object.keys(keywords) as (keyof ChunkHits)[]) keywords[k] = uniq(keywords[k]);

    return {
      hash: crypto.createHash("sha1").update(normalizeForHash(evidence)).digest("hex").slice(0, 16),
      score: Math.max(...g.map((i) => scored[i].score)),
      section: chunks[g[0]].section,
      evidence,
      context: r.text,
      keywords,
      reasons: uniq(g.flatMap((i) => scored[i].reasons)),
      hints: matcher.hints(r.text),
      chunkRange: [r.start, r.end],
    };
  });
}
