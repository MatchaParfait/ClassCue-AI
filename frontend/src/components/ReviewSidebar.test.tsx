import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { ReviewSidebar } from "./ReviewSidebar";
import type { Marker, MarkerMode } from "../types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import * as questionVisuals from "../api/questionVisuals";

describe("recall review navigation", () => {
  it("groups built-in and custom modes, keeps questions separate and supports drawer keyboard navigation", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const client = new QueryClient();
    const custom = `custom_${"a".repeat(32)}` as const;
    const other = `custom_${"b".repeat(32)}` as const;
    const name = "这是一项名称很长的自定义教师考点提取方案";
    client.setQueryData(["settings", "recall-schemes"], [
      { id: custom, name, archived: false }, { id: other, name: "另一个方案", archived: false },
    ]);
    const change = vi.fn();
    const marker: Marker = {
      id: 1, video_id: 1, start_ms: 0, end_ms: 1000, title: "排序", note: "", source_quote: "",
      analysis_mode: custom, review_status: "active",
    };
    const render = (mode: MarkerMode) => act(() => root.render(<QueryClientProvider client={client}>
      <ReviewSidebar mode={mode} onModeChange={change} status="all" onStatusChange={vi.fn()}
        markers={[]} allMarkers={[marker, { ...marker, id: 2, review_status: "excluded" }]} excludedMarkers={[]}
        selectedMarkerId={null} viewedMarkerIds={new Set()} onSelectMarker={vi.fn()} onChangeStatus={vi.fn()}
        onUndo={vi.fn()} canUndo={false} onRestoreExcluded={vi.fn()} onPrevious={vi.fn()} onNext={vi.fn()}
        editingMarkerId={null} timeSaving={false} onEditMarker={vi.fn()} durationMs={10000} currentTimeMs={0}
        getCurrentTimeMs={() => 0} onSaveTime={vi.fn()} onChangeImportance={vi.fn()} />
    </QueryClientProvider>));
    try {
      render("medium_recall");
      const drawers = container.querySelectorAll<HTMLElement>(".recall-mode-drawer");
      const disclosures = [...drawers].map(drawer => drawer.querySelector<HTMLButtonElement>(".recall-mode-toggle")!);
      expect(drawers).toHaveLength(2);
      expect(disclosures[0].getAttribute("aria-expanded")).toBe("false");
      expect(drawers[0].querySelector(".recall-mode-options")?.hasAttribute("hidden")).toBe(true);
      expect(drawers[0].querySelectorAll('[role="tab"]')).toHaveLength(3);
      expect(drawers[1].querySelectorAll('[role="tab"]')).toHaveLength(2);
      expect(container.querySelector(".question-mode")?.closest("details")).toBeNull();
      expect(container.querySelector(".question-mode button")?.textContent).toBe("题目提取0");
      const direct = drawers[0].querySelector<HTMLButtonElement>(".recall-mode-direct")!;
      expect(direct.title).toBe("直接切换到中召回");
      act(() => direct.click());
      expect(change).toHaveBeenLastCalledWith("medium_recall");
      expect(disclosures[0].getAttribute("aria-expanded")).toBe("false");
      change.mockClear();
      act(() => disclosures[0].click());
      expect(disclosures[0].getAttribute("aria-expanded")).toBe("true");
      expect(change).not.toHaveBeenCalled();
      const high = drawers[0].querySelector<HTMLButtonElement>('button[title="高召回"]')!;
      act(() => high.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
      expect(change).toHaveBeenLastCalledWith("medium_recall");
      expect(document.activeElement?.textContent).toBe("中召回0");
      act(() => high.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
      expect(change).toHaveBeenLastCalledWith("low_recall");
      render("low_recall");
      render("question_extraction");
      expect(direct.title).toBe("直接切换到低召回");
      act(() => direct.click());
      expect(change).toHaveBeenLastCalledWith("low_recall");
      act(() => high.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
      expect(disclosures[0].getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(disclosures[0]);
      render(custom);
      expect(drawers[1].classList.contains("is-current")).toBe(true);
      expect(drawers[1].querySelector(".recall-mode-current")?.textContent).toBe(name);
      const customButton = drawers[1].querySelector<HTMLButtonElement>('[role="tab"]')!;
      expect(customButton.title).toBe(name);
      expect(customButton.textContent).toBe(`${name}1`);
      await act(async () => {
        client.setQueryData(["settings", "recall-schemes"], [{ id: custom, name, archived: false }]);
        await new Promise(resolve => setTimeout(resolve, 0));
      });
      expect(container.querySelectorAll(".recall-mode-drawer")).toHaveLength(1);
      expect(container.querySelector('[aria-label="自定义召回"]')?.closest("details")).toBeNull();
      expect(container.querySelector('[aria-label="自定义召回"] button[aria-selected="true"]')?.textContent).toBe(`${name}1`);
      act(() => container.querySelector<HTMLButtonElement>(".question-mode button")!.click());
      expect(change).toHaveBeenLastCalledWith("question_extraction");
    } finally {
      window.getSelection()?.removeAllRanges();
      act(() => root.unmount());
      client.clear();
      container.remove();
    }
  });

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
      onSearchBilibili: vi.fn(),
    };
    try {
      render(<ReviewSidebar {...props} />);
      expect(container.querySelectorAll(".mode-switcher [role=tab]").length).toBe(4);
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
      expect(container.querySelector('[title^="查找 B 站讲解"]')).toBeNull();
      expect(container.textContent).not.toContain("B 站讲解");
      expect(props.onSearchBilibili).not.toHaveBeenCalled();
      expect(props.onSelectMarker).not.toHaveBeenCalled();
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
      act(() => (container.querySelector('.mode-switcher [role="tab"]') as HTMLButtonElement).click());
      expect(change).toHaveBeenCalledWith("high_recall");
      render(<ReviewSidebar {...props} allMarkers={[
        ...markers, { ...markers[0], id: 2, analysis_mode: "strict_exam_intent" },
      ]} />);
      expect(container.querySelectorAll(".mode-switcher [role=tab]").length).toBe(5);
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
      const visualRequests = vi.spyOn(questionVisuals, "getQuestionVisuals")
        .mockResolvedValue({ items: [], active: false });
      const assignments = vi.spyOn(questionVisuals, "assignQuestionImages")
        .mockResolvedValue({ items: [], active: false });
      client.setQueryData(["settings", "recall-schemes"], []);
      const question: Marker = { ...markers[0], analysis_mode: "question_extraction" };
      try {
        render(<ReviewSidebar {...props} analysisVideoId={1} mode="question_extraction"
          markers={[question]} allMarkers={[question]} />);
        expect(container.querySelector(".question-visual-button")).not.toBeNull();
        expect(visualRequests).toHaveBeenCalledWith(1);
        act(() => container.querySelector<HTMLButtonElement>(".marker-time-jump")!.click());
        expect(props.onSelectMarker).toHaveBeenCalledWith(question);
      } finally {
        visualRequests.mockRestore();
        assignments.mockRestore();
      }
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
