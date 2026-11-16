// Client-side checks for the change-password form. Pure. Supabase enforces the minimum length too
// (set it to 12 in the Auth settings); these checks only give quick, generic feedback.
export const MIN_PASSWORD_LENGTH = 12;

export const PW_CURRENT_REQUIRED = "Enter your current password.";
export const PW_TOO_SHORT = `New password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
export const PW_MISMATCH = "New password and confirmation do not match.";
export const PW_SAME = "New password must be different from the current one.";
export const PW_CURRENT_WRONG = "Current password is incorrect.";
export const PW_FAILED = "Could not change the password. Try again.";
export const PW_CHANGED = "Password changed.";

export function validatePasswordChange(input: { current: string; next: string; confirm: string }): string | null {
  if (input.current === "") return PW_CURRENT_REQUIRED;
  if (input.next.length < MIN_PASSWORD_LENGTH) return PW_TOO_SHORT;
  if (input.next !== input.confirm) return PW_MISMATCH;
  if (input.next === input.current) return PW_SAME;
  return null;
}
