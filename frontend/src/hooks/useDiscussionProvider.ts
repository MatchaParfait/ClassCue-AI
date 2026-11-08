import { useSyncExternalStore } from "react";

const key = "classcue-discussion-provider";
const event = "classcue-discussion-provider-change";
export type DiscussionProvider = "dsh" | "codex";
function current(): DiscussionProvider {
  try { return localStorage.getItem(key) === "codex" ? "codex" : "dsh"; } catch { return "dsh"; }
}
function subscribe(listener: () => void) {
  window.addEventListener(event, listener);
  window.addEventListener("storage", listener);
  return () => { window.removeEventListener(event, listener); window.removeEventListener("storage", listener); };
}
export function setDiscussionProvider(value: DiscussionProvider) {
  localStorage.setItem(key, value);
  window.dispatchEvent(new Event(event));
}
export const useDiscussionProvider = () => useSyncExternalStore(subscribe, current, current);
