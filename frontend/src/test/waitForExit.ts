import { act } from "react";
import { expect, vi } from "vitest";

// Wait for removal rather than an implementation-specific timer. During an exit
// the element may still be painted, while interaction is already disabled.
export async function waitForExit(container: HTMLElement, selector: string) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(container.querySelector(selector)).toBeNull();
  });
}
