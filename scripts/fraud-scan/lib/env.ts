import fs from "fs";
import path from "path";

/**
 * .env.local → .env 순으로 읽어 아직 설정되지 않은 변수만 채운다 (Next.js와 같은 우선순위).
 * 이미 셸/배치에서 설정한 환경변수가 항상 이긴다.
 */
export function loadEnv(cwd: string = process.cwd()): void {
  for (const envFile of [".env.local", ".env"]) {
    const envPath = path.join(cwd, envFile);
    if (!fs.existsSync(envPath)) continue;
    for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
      const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*?)\s*$/);
      if (match && !process.env[match[1]]) process.env[match[1]] = match[2].replace(/^["']|["']$/g, "");
    }
  }
}

/** DART_API_KEY 우선, 없으면 기존 프로젝트 변수명 OPENDART_API_KEY */
export function getDartApiKey(): string {
  return process.env.DART_API_KEY || process.env.OPENDART_API_KEY || "";
}
