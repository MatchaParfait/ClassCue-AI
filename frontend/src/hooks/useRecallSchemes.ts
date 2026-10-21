import { useQuery } from "@tanstack/react-query";
import { getRecallSchemes } from "../api/client";
import { RECALL_MODES } from "../processOptions";
import type { MarkerMode, RecallScheme } from "../types";

export const recallSchemesKey = ["settings", "recall-schemes"] as const;

export function useRecallSchemes(active = true) {
  return useQuery({ queryKey: recallSchemesKey, queryFn: getRecallSchemes, enabled: active, staleTime: 30_000 });
}

export function recallLabel(mode: MarkerMode, schemes: Pick<RecallScheme, "id" | "name">[] = []) {
  return RECALL_MODES.find(item => item.value === mode)?.label
    ?? schemes.find(item => item.id === mode)?.name
    ?? (mode === "strict_exam_intent" ? "旧版考点" : "自定义方案");
}
