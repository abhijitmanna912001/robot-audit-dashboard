# Robot Audit Dashboard

Next.js (App Router, TypeScript, Tailwind) dashboard for an auditable robot agent.
Hackathon project (Cyberwave x Supabase meetup, Sat 14 Nov). Simulation only.

## How the system works

Dashboard writes a command row to Supabase. A Python worker (built by my teammate)
picks it up, plans actions, validates them, runs them in a Cyberwave simulation and
logs results. A safety critic (my route handler at /api/critic) reviews the plan.
A deterministic Python validator is the mandatory execution gate. If the critic
fails or returns invalid output, nothing executes (fail closed).

## My responsibilities (this repo)

- Dashboard UI, Supabase Realtime, command submission route, /api/critic, Vercel deploy

## Data

- Tables: `commands` (current status) and `audit_events` (append-only history)
- Statuses: approved, rejected, executing, completed, failed
- Action and verdict JSON schemas are still being agreed. Do not invent them. Use
  typed mock data until I confirm.

## Rules

- Browser uses only the anon key. Service-role and OpenAI keys are server-only,
  never in NEXT*PUBLIC* variables.
- Never read .env files.
- Explain tradeoffs before big decisions. Keep changes small and tell me what you changed and why.
- Keep it simple. Do not add libraries without asking.
- Never kill processes you did not start. Never run pkill.
