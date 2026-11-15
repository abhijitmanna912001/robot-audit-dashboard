// POST /api/commands: signed-in, allowlisted operators only, rate limited. The logic and its
// check order live in src/lib/submit-command.ts (testable without Next); this file only wires the
// real session, database client and environment.
import { cookies } from "next/headers";
import { handleSubmit } from "../../../lib/submit-command.ts";
import { createServerClient } from "../../../lib/supabase/server.ts";
import { createAuthServerClient } from "../../../lib/supabase/auth-server.ts";

export async function POST(request: Request) {
  return handleSubmit(request, {
    getUser: async () => {
      const auth = createAuthServerClient(await cookies());
      // getUser() asks the Auth server to validate the token. Never getSession().
      const { data, error } = await auth.auth.getUser();
      return error || !data.user ? null : { id: data.user.id, email: data.user.email };
    },
    db: createServerClient,
    operatorsRaw: process.env.OPERATORS,
    now: () => new Date(),
  });
}
