"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { caseHeaderLayout } from "@/lib/case-header";

type Placement = { top: number; left: number; width: number };

// UI geometry only. The case content remains server-renderable children; this component
// observes scrolling and resizing, without reading or changing any application data.
export function CaseFileFrame({ titleBlock, compactHeader, children }: {
  titleBlock: ReactNode; compactHeader: ReactNode; children: ReactNode;
}) {
  const card = useRef<HTMLElement>(null);
  const title = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const measure = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);

  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      if (!card.current || !title.current || !content.current || !measure.current) return;
      let pane: HTMLElement | null = card.current.parentElement;
      while (pane && !/(auto|scroll)/.test(getComputedStyle(pane).overflowY)) pane = pane.parentElement;
      const paneRect = pane?.getBoundingClientRect();
      const appBar = pane ? null : document.querySelector<HTMLElement>("[data-dashboard-header]");
      const top = paneRect ? Math.max(0, paneRect.top + (pane?.clientTop ?? 0)) : Math.max(0, appBar?.getBoundingClientRect().bottom ?? 0);
      const bottom = paneRect ? Math.min(innerHeight, paneRect.bottom) : innerHeight;
      const bounds = card.current.getBoundingClientRect();
      const height = measure.current.offsetHeight;
      const layout = caseHeaderLayout(title.current.getBoundingClientRect().bottom, bounds, top, bottom, height);
      // Clip the title AND evidence precisely below the opaque header. The header is
      // portalled outside this clipping layer, and cannot inherit an ancestor's opacity.
      content.current.style.clipPath = layout ? `inset(${layout.clip}px 0 0 0)` : "";
      const scroller = pane ?? document.documentElement;
      const clearance = height + 12 + (pane ? 0 : top);
      const previous = parseFloat(scroller.style.getPropertyValue("--case-header-clearance")) || 0;
      scroller.style.setProperty("--case-header-clearance", `${Math.max(previous, clearance)}px`);
      setPlacement((old) => {
        if (!layout) return old === null ? old : null;
        if (old?.top === layout.top && old.left === layout.left && old.width === layout.width) return old;
        return { top: layout.top, left: layout.left, width: layout.width };
      });
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    const observer = new ResizeObserver(schedule);
    if (card.current) observer.observe(card.current);
    if (title.current) observer.observe(title.current);
    if (measure.current) observer.observe(measure.current);
    const appBar = document.querySelector("[data-dashboard-header]");
    if (appBar) observer.observe(appBar);
    // Capture scrolls in either dashboard pane, the preview pane, or the document.
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, []);

  return (
    <article ref={card} className="@container surface-card relative min-w-0 rounded-ui-lg" aria-label="Case file">
      <div ref={measure} aria-hidden="true" className="case-header-measure">{compactHeader}</div>
      <div ref={content}>
        <div ref={title} className="px-5 pt-5 pb-6 @case:px-8 @case:pt-8">{titleBlock}</div>
        <div className="flex min-w-0 flex-col gap-6 px-5 pb-5 @case:px-8 @case:pb-8">{children}</div>
      </div>
      {placement && createPortal(
        <div className="case-floating-header" style={placement}>{compactHeader}</div>,
        document.body,
      )}
    </article>
  );
}
