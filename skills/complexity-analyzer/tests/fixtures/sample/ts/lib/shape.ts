export interface Rect {
  w: number;
  h: number;
}

export function area(s: Rect): number {
  return s.w * s.h;
}

export function unusedRectHelper(a: Rect, b: Rect): number {
  return area(a) + area(b);
}

export type UnusedAlias = Rect[];
