// Keyboard movement through a vertical list. Pure. Returns the new index, or undefined when the
// key is not a list key (so the caller leaves the event alone).
export function stepIndex(length: number, index: number, key: string): number | undefined {
  if (!Number.isInteger(length) || length <= 0) return undefined;
  const last = length - 1;
  const at = Number.isInteger(index) && index >= 0 && index <= last ? index : 0;
  switch (key) {
    case "ArrowDown":
      return at === last ? 0 : at + 1;
    case "ArrowUp":
      return at === 0 ? last : at - 1;
    case "Home":
      return 0;
    case "End":
      return last;
    default:
      return undefined;
  }
}
