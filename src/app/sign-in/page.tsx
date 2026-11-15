import { Suspense } from "react";
import Link from "next/link";
import { Brand } from "@/components/Header";
import { SignInForm } from "./SignInForm";

export const metadata = { title: "Sign in" };

type SearchParams = Promise<{ error?: string }>;

// Reads searchParams (runtime data), so it sits inside <Suspense>.
async function Form({ searchParams }: { searchParams: SearchParams }) {
  const { error } = await searchParams;
  return <SignInForm failedBefore={error === "1"} />;
}

export default function SignInPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-md flex-col gap-8 px-4 py-10">
      <Brand />
      <section aria-label="Sign in" className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold">Sign in to submit commands</h2>
        <p className="text-sm text-muted-foreground">
          Only invited operators can submit commands. Reading the dashboard needs no account.
        </p>
        <Suspense fallback={null}>
          <Form searchParams={searchParams} />
        </Suspense>
      </section>
      <Link href="/" className="text-sm text-primary underline underline-offset-4">
        Back to the dashboard
      </Link>
    </main>
  );
}
