import { b } from './b.js';

export function a(n) {
  if (n > 0) {
    return b(n - 1) + 1;
  }
  return 0;
}
