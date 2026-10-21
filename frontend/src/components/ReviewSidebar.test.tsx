import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { ReviewSidebar } from "./ReviewSidebar";
import type { Marker } from "../types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

describe("recall review navigation", () => {
  it("keeps three modes separate and displays legacy results only when present", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const client = new QueryClient();
    const render = (node: ReactNode) => act(() => root.render(<QueryClientProvider client={client}>{node}</QueryClientProvider>));
    const change = vi.fn();
    const markers: Marker[] = [
      { id: 1, video_id: 1, start_ms: 0, end_ms: 1000, title: "排序", note: "理解排序过程", source_quote: "把排序的过程画下来", analysis_mode: "low_recall", review_status: "active" },
    ];
    const props = {
      mode: "low_recall" as const, onModeChange: change, status: "all" as const,
      onStatusChange: vi.fn(), markers, allMarkers: markers, excludedMarkers: [],
      selectedMarkerId: null, viewedMarkerIds: new Set<number>(), onSelectMarker: vi.fn(), onChangeStatus: vi.fn(),
      onUndo: vi.fn(), canUndo: false, onRestoreExcluded: vi.fn(),
      onPrevious: vi.fn(), onNext: vi.fn(),
      editingMarkerId: null, timeSaving: false, onEditMarker: vi.fn(), durationMs: 10000, currentTimeMs: 0,
      getCurrentTimeMs: () => 0, onSaveTime: vi.fn().mockResolvedValue(undefined),
      onChangeImportance: vi.fn().mockResolvedValue(undefined),
    };
    try {
      render(<ReviewSidebar {...props} />);
      expect(container.querySelectorAll(".mode-switcher [role=tab]").length).toBe(3);
      expect(container.querySelector(".mode-switcher [aria-selected=true]")?.textContent).toBe("低召回1");
      expect(container.textContent).not.toContain("旧版考点");
      const jump = container.querySelector<HTMLButtonElement>(".marker-main")!;
      expect(jump.title).toBe("跳转到 0:00");
      expect(jump.querySelector('.marker-jump-indicator[aria-hidden="true"]')).not.toBeNull();
      act(() => jump.click());
      expect(props.onSelectMarker).toHaveBeenCalledTimes(1);
      expect(props.onSelectMarker).toHaveBeenCalledWith(markers[0]);
      props.onSelectMarker.mockClear();
      act(() => container.querySelector<HTMLButtonElement>(".marker-time-jump")!.click());
      expect(props.onSelectMarker).toHaveBeenCalledTimes(1);
      expect(props.onSelectMarker).toHaveBeenCalledWith(markers[0]);
      props.onSelectMarker.mockClear();
      for (const selector of [".marker-main strong", ".marker-note", ".marker-quote", ".marker-evidence", ".marker-item"]) {
        act(() => container.querySelector<HTMLElement>(selector)!.click());
        expect(props.onSelectMarker).toHaveBeenCalledTimes(1);
        expect(props.onSelectMarker).toHaveBeenCalledWith(markers[0]);
        props.onSelectMarker.mockClear();
      }
      const selection = window.getSelection()!;
      const range = document.createRange();
      range.selectNodeContents(container.querySelector(".marker-quote")!);
      selection.addRange(range);
      try {
        act(() => container.querySelector<HTMLElement>(".marker-quote")!.click());
        expect(props.onSelectMarker).not.toHaveBeenCalled();
      } finally {
        selection.removeAllRanges();
      }
      act(() => container.querySelector<HTMLButtonElement>(".marker-edit-time")!.click());
      expect(props.onEditMarker).toHaveBeenCalledWith(markers[0].id);
      act(() => container.querySelector<HTMLButtonElement>(".danger-action")!.click());
      expect(props.onChangeStatus).toHaveBeenCalledWith(markers[0], "excluded");
      expect(props.onSelectMarker).not.toHaveBeenCalled();
      await act(async () => container.querySelector<HTMLButtonElement>("button.importance-critical")!.click());
      expect(props.onChangeImportance).toHaveBeenCalledWith(markers[0], "critical");
      expect(props.onSelectMarker).not.toHaveBeenCalled();
      render(<ReviewSidebar {...props} editingMarkerId={markers[0].id} />);
      act(() => container.querySelector<HTMLElement>(".marker-time-editor")!.click());
      act(() => container.querySelector<HTMLLabelElement>(".marker-time-editor label")!.click());
      expect(props.onSelectMarker).not.toHaveBeenCalled();
      render(<ReviewSidebar {...props} />);
      act(() => (container.querySelector(".mode-switcher button") as HTMLButtonElement).click());
      expect(change).toHaveBeenCalledWith("high_recall");
      render(<ReviewSidebar {...props} allMarkers={[
        ...markers, { ...markers[0], id: 2, analysis_mode: "strict_exam_intent" },
      ]} />);
      expect(container.querySelectorAll(".mode-switcher [role=tab]").length).toBe(4);
      expect(container.textContent).toContain("旧版考点");
      const quote = "教师要求能够在理解的基础上设计算法，并说明分析问题和验证算法的方法。".repeat(5);
      render(<ReviewSidebar {...props} markers={[{ ...markers[0], source_quote: quote }]} />);
      const toggle = container.querySelector<HTMLButtonElement>(".quote-toggle")!;
      expect(toggle.textContent).toBe("展开原文");
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(container.querySelector(`#${toggle.getAttribute("aria-controls")}`)?.textContent).toContain(quote);
      act(() => toggle.click());
      expect(toggle.textContent).toBe("收起原文");
      expect(toggle.getAttribute("aria-expanded")).toBe("true");
      expect(props.onSelectMarker).not.toHaveBeenCalled();
      act(() => container.querySelector<HTMLElement>(".marker-quote")!.click());
      expect(props.onSelectMarker).toHaveBeenCalledTimes(1);
      expect(props.onSelectMarker).toHaveBeenCalledWith(expect.objectContaining({ id: markers[0].id, source_quote: quote }));
      props.onSelectMarker.mockClear();
      act(() => toggle.click());
      expect(toggle.textContent).toBe("展开原文");
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(props.onSelectMarker).not.toHaveBeenCalled();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
