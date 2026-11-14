// Static explainer, below the case file. No data, nothing here is derived from events.
// Every line states what the system does today (see CLAUDE.md): the validator is the mandatory
// gate, the critic fails closed, execution is simulation only, the audit log is append-only.
const STEPS: { name: string; text: string }[] = [
  { name: "Plan", text: "Parsed deterministically from the command text." },
  { name: "Validator", text: "Checks every action against the robot's joint limits." },
  {
    name: "Critic",
    text: "An independent service that can only reject or approve. Any error means nothing executes.",
  },
  { name: "Execution", text: "Dry run: nothing is sent to a robot." },
];
const CLOSING = "Nothing executes unless both checks approve. Every event is recorded.";
const TITLE = "How a command is checked";

// One collapsed details line, closed by default at every width.
export function HowItWorks() {
  return (
    <details className="group mt-6 border-t border-border pt-2">
      <summary className="flex min-h-9 cursor-pointer list-none items-center gap-2 rounded-ui-sm font-mono text-xs leading-4 font-medium tracking-[0.12em] text-muted-foreground uppercase outline-none select-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/30 [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="inline-block transition-transform group-open:rotate-90">
          ▸
        </span>
        {TITLE}
      </summary>
      <div className="surface-card mt-2 rounded-ui-md p-4">
        <ol className="flex flex-col gap-2">
          {STEPS.map((s, i) => (
            <li key={s.name} className="flex gap-3">
              <span
                aria-hidden="true"
                className="flex size-5 shrink-0 items-center justify-center rounded-full border border-border font-mono text-xs leading-4 text-muted-foreground"
              >
                {i + 1}
              </span>
              <p className="min-w-0 text-sm leading-5 text-muted-foreground">
                <span className="font-medium text-foreground">{s.name}</span> {s.text}
              </p>
            </li>
          ))}
        </ol>
        <p className="mt-3 text-sm leading-5 text-muted-foreground">{CLOSING}</p>
      </div>
    </details>
  );
}
