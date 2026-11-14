# Robot Audit Dashboard

Next.js (App Router, TypeScript, Tailwind) dashboard for an auditable robot agent.
Hackathon project (Cyberwave x Supabase meetup, Sat 14 Nov). Simulation only.

## How the system works

The dashboard submits a command through the server route POST /api/commands, which inserts the command row and its command_received event into Supabase. A Python worker (built by my teammate)
picks it up, plans actions, validates them, runs them in a Cyberwave simulation and
logs results. A safety critic (my route handler at /api/critic) reviews the plan.
A deterministic Python validator is the mandatory execution gate. If the critic
fails or returns invalid output, nothing executes (fail closed).

## My responsibilities (this repo)

- Dashboard UI, Supabase Realtime, command submission route, /api/critic, Vercel deploy

## Data

- Tables: `commands` (current status) and `audit_events` (append-only history)
- Statuses: received, approved, rejected, executing, completed, failed
- Contracts are in docs/schema-draft.md and db/001_init.sql (hash contract, critic request and response, event names). The SQL is applied to Supabase, so do not change it without asking.
- The action vocabulary (action types, joint names, limits) is still open until docs/robot-capabilities.md exists. Do not invent action types or joint names.
- audit_events is append-only (a trigger blocks UPDATE and DELETE). Never write code or tests that create rows against the real database without asking.

## Rules

- Browser uses only the anon key. Service-role and OpenAI keys are server-only,
  never in NEXT*PUBLIC* variables.
- Never read .env files.
- Explain tradeoffs before big decisions. Keep changes small and tell me what you changed and why.
- Keep it simple. Do not add libraries without asking.
- Never kill processes you did not start. Never run pkill.
- /api/critic needs the x-critic-secret header, verifies planHash (RFC 8785 + SHA-256, see src/lib/plan-hash.ts), and must fail closed: every error path returns a reject, never an approval.
- Never log or return secrets, raw database errors or keys.
- POST /api/commands can return 500 with a commandId in the body. That means the command was saved but its audit event failed, so the UI must not resubmit. A 500 without a commandId means nothing was saved.
- shadcn/ui components are allowed (button, badge, card, textarea, sonner). No other new libraries without asking.