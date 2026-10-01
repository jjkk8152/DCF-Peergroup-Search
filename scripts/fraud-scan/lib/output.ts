import fs from "fs";

export function csvCell(v: unknown): string {
  const s = v == null ? "" : Array.isArray(v) ? v.join("; ") : typeof v === "object" ? JSON.stringify(v) : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 엑셀에서 한글이 깨지지 않도록 UTF-8 BOM 포함 CSV */
export function writeCsv(file: string, header: string[], rows: Record<string, unknown>[]): void {
  const lines = [header.join(",")];
  for (const r of rows) lines.push(header.map((h) => csvCell(r[h])).join(","));
  fs.writeFileSync(file, "﻿" + lines.join("\r\n"));
}

export function readJsonl<T>(file: string): T[] {
  if (!fs.existsSync(file)) return [];
  const out: T[] = [];
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      out.push(JSON.parse(line));
    } catch {
      // 중단 시 마지막 줄이 잘렸을 수 있음 — 무시
    }
  }
  return out;
}

export function appendJsonl(file: string, obj: unknown): void {
  fs.appendFileSync(file, JSON.stringify(obj) + "\n");
}

export function writeJsonl(file: string, rows: unknown[]): void {
  fs.writeFileSync(file, rows.map((r) => JSON.stringify(r)).join("\n") + (rows.length ? "\n" : ""));
}

export function makeLogger(file: string) {
  return (msg: string) => {
    const line = `[${new Date().toISOString()}] ${msg}`;
    console.log(msg);
    fs.appendFileSync(file, line + "\n");
  };
}
