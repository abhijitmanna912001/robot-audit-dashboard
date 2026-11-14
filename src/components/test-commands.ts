// Which commands count as "test commands" for the optional hide toggle. Pure.
// Deliberately narrow: only these two shapes, nothing else is ever hidden.
//   1. the text starts with "[smoke test]" (leading whitespace ignored)
//   2. the text is made only of the lowercase letter "a" (surrounding whitespace ignored)
export function isTestCommand(text: unknown): boolean {
  if (typeof text !== "string") return false;
  if (text.trimStart().startsWith("[smoke test]")) return true;
  return /^a+$/.test(text.trim());
}
