"use client";

import { TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";

// Route-level error boundary. It never shows or logs the error itself: messages can carry
// raw database or server details.
export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-12">
      <div role="alert" className="flex w-full max-w-md flex-col items-center gap-4 surface-card rounded-ui-lg px-6 py-10 text-center">
        <span aria-hidden="true" className="flex size-10 items-center justify-center rounded-full bg-status-failed-accent/15 text-status-failed-accent">
          <TriangleAlert className="size-5" />
        </span>
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold text-balance">Something went wrong</h1>
          <p className="text-sm text-muted-foreground">
            The dashboard could not load. Your commands and audit trail are not affected.
          </p>
        </div>
        <Button type="button" onClick={() => reset()}>
          Try again
        </Button>
      </div>
    </main>
  );
}
