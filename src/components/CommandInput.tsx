"use client";

import { useState } from "react";

export function CommandInput() {
  const [text, setText] = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    // TODO: submit to the command route once it exists.
    setText("");
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 sm:flex-row">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={2}
        placeholder="Type a command for the robot..."
        className="flex-1 rounded-md border border-zinc-300 bg-transparent p-2 text-sm dark:border-zinc-700"
      />
      <button
        type="submit"
        className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        Submit
      </button>
    </form>
  );
}
