// Static explainer for the left column. No data, nothing here is derived from events.
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

function Body() {
  return (
    <>
      <ol className="flex flex-col gap-2">
        {STEPS.map((s, i) => (
          <li key={s.name} className="flex gap-3">
            <span
              aria-hidden="true"
              className="flex size-5 shrink-0 items-center justify-center rounded-full border border-border font-mono text-[11px] leading-4 text-muted-foreground"
            >
              {i + 1}
            </span>
            <p className="min-w-0 text-xs leading-4 text-muted-foreground">
              <span className="font-medium text-foreground">{s.name}</span> {s.text}
            </p>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs leading-4 text-muted-foreground">{CLOSING}</p>
    </>
  );
}

// Open card on desktop, collapsed details on small screens. Two renderings of one source, one
// of them always display:none, so there is no duplicate for assistive technology.
export function HowItWorks() {
  return (
    <>
      <section aria-label={TITLE} className="surface-card hidden rounded-ui-lg p-4 lg:block">
        <h2 className="mb-3 text-[11px] leading-4 font-medium tracking-[0.14em] text-muted-foreground uppercase">
          {TITLE}
        </h2>
        <Body />
      </section>
      <details className="surface-card group rounded-ui-lg p-4 lg:hidden">
        <summary className="flex cursor-pointer list-none items-center justify-between text-[11px] leading-4 font-medium tracking-[0.14em] text-muted-foreground uppercase outline-none select-none focus-visible:ring-3 focus-visible:ring-ring/50 [&::-webkit-details-marker]:hidden">
          {TITLE}
          <span aria-hidden="true" className="transition-transform duration-150 group-open:rotate-180">
            ▾
          </span>
        </summary>
        <div className="mt-3">
          <Body />
        </div>
      </details>
    </>
  );
}
