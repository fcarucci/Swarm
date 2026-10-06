import { score } from "@lib/foo";
import { area } from "./lib/shape";
import type { Rect } from "./lib/shape";

export function classify(n: number, shapes: Rect[]): string {
  let out = "";
  for (const s of shapes) {
    if (area(s) > n) {
      if (score(n) > 1 && n % 2 === 0) {
        out += "big";
      } else {
        out += "mid";
      }
    }
  }
  const widths = shapes.map((s) => (s.w > 1 ? 1 : 0));
  return out + widths.length;
}
