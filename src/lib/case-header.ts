// Geometry for the display-only compact case header. Coordinates are viewport pixels.
export type CaseHeaderBounds = { top: number; bottom: number; left: number; width: number };
export function caseHeaderLayout(titleBottom: number, card: CaseHeaderBounds, top: number, bottom: number, height: number) {
  if (![titleBottom, card.top, card.bottom, card.left, card.width, top, bottom, height].every(Number.isFinite)) return null;
  if (height <= 0 || card.width <= 0 || titleBottom > top || card.bottom <= top + height || bottom - top < height) return null;
  return { top, left: card.left, width: card.width, clip: Math.max(0, top + height - card.top) };
}
