import Link from "next/link";
import { SearchX } from "lucide-react";
import { buttonVariants } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-12">
      <div className="flex w-full max-w-md flex-col items-center gap-4 surface-card rounded-ui-lg px-6 py-10 text-center">
        <span aria-hidden="true" className="flex size-10 items-center justify-center rounded-full bg-primary/15 text-primary">
          <SearchX className="size-5" />
        </span>
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold text-balance">Page not found</h1>
          <p className="text-sm text-muted-foreground">That page does not exist.</p>
        </div>
        <Link href="/" className={buttonVariants()}>
          Back to the dashboard
        </Link>
      </div>
    </main>
  );
}
