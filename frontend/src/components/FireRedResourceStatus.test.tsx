import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { getAsrResourceProfile } from "../api/client";
import { FireRedResourceProfile } from "./FireRedResourceStatus";

vi.mock("../api/client", () => ({ getAsrResourceProfile: vi.fn() }));

it("does not inspect GPU resources while its retained panel is hidden", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(getAsrResourceProfile).mockResolvedValue({ engine: "firered", device: "gpu", initial_batch_size: 4 });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const render = (active: boolean) => act(() => root.render(
    <QueryClientProvider client={client}><FireRedResourceProfile modelId="firered" active={active} /></QueryClientProvider>,
  ));
  try {
    render(false);
    expect(getAsrResourceProfile).not.toHaveBeenCalled();
    render(true);
    await vi.waitFor(async () => {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
      expect(getAsrResourceProfile).toHaveBeenCalledTimes(1);
    });
    render(false);
    await act(async () => { await client.invalidateQueries({ queryKey: ["settings", "asr", "resource-profile"] }); });
    expect(getAsrResourceProfile).toHaveBeenCalledTimes(1);
  } finally {
    act(() => root.unmount());
    client.clear();
    container.remove();
  }
});
