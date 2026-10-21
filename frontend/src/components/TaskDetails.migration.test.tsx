import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { TaskDetails } from "./TaskDetails";

it("opens storage settings for a migration and does not offer an unsupported stop command", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient();
  const onOpen = vi.fn();
  const job = { id: "migration", job_type: "storage_migration", status: "running", progress: 45, message: "正在复制文件" };
  try {
    act(() => root.render(<QueryClientProvider client={client}><TaskDetails job={job} loading={false} error={null}
      onRetryLoad={vi.fn()} onOpen={onOpen} onSettings={vi.fn()} onJob={vi.fn()} /></QueryClientProvider>));
    expect(container.textContent).toContain("目录迁移");
    expect(container.textContent).not.toContain("停止任务");
    const button = [...container.querySelectorAll("button")].find(item => item.textContent === "打开存储设置");
    expect(button).toBeTruthy();
    act(() => button!.click());
    expect(onOpen).toHaveBeenCalledWith(job);
  } finally {
    await act(async () => { root.unmount(); client.clear(); });
    container.remove();
  }
});
