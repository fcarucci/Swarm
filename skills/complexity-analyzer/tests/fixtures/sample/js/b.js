import { a } from './a.js';

export function b(n) {
  if (n > 0) {
    return a(n - 1) + 1;
  }
  return 0;
}
