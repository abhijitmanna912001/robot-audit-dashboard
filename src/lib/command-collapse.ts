// Display only: which rows the narrow (below lg) layout hides until "Show all". Pure.
// The first `limit` rows stay in list order; the selected command is never hidden, so the open
// case file always keeps its row (same rule as filterCommands).
export const COLLAPSED_LIMIT = 5;

export function collapsedIds(
  commands: readonly { id: string }[],
  selectedId: string | null | undefined,
  expanded: boolean,
  limit: number = COLLAPSED_LIMIT,
): Set<string> {
  const hidden = new Set<string>();
  if (expanded) return hidden;
  commands.forEach((c, i) => {
    if (i >= limit && c.id !== selectedId) hidden.add(c.id);
  });
  return hidden;
}
