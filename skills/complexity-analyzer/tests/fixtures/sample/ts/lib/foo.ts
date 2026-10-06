import type { Rect } from "./shape";

export function score(n: number): number {
  return n > 10 ? 2 : 1;
}

export function describe(s: Rect): string {
  return String(s.w);
}
