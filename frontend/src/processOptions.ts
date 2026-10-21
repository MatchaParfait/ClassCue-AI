import type { RecallMode } from "./types";

export const RECALL_SELECTION_REQUIRED = "请至少选择一种考点召回类型后再开始处理";

export const RECALL_MODES: { value: RecallMode; label: string }[] = [
  { value: "high_recall", label: "高召回" },
  { value: "medium_recall", label: "中召回" },
  { value: "low_recall", label: "低召回" },
];

export type ProcessForm = {
  displayName: string;
  recallModes: RecallMode[];
  keywords: string;
  filterFormula: string;
  filterLowInformation: boolean;
  forceRetranscribe?: boolean;
  qwenMode?: "auto" | "low-resource" | "manual";
  qwenBatchSize?: number;
  qwenChunkSeconds?: number;
  qwenAlignBatchSize?: number;
  qwenLowMemory?: boolean;
  qwenAutoRecovery?: boolean;
};

export function buildProcessPayload(form: ProcessForm) {
  const keywords = [...new Set(form.keywords.split(/[,，;；\n]+/).map((value) => value.trim()).filter(Boolean))];
  const modes = [...new Set(form.recallModes ?? ["medium_recall"])];
  if (!modes.length) throw new Error(RECALL_SELECTION_REQUIRED);
  return {
    analysis_mode: modes[0],
    analysis_modes: modes,
    keywords,
    use_keyword_prefilter: keywords.length > 0,
    filter_formula: form.filterFormula.trim() || null,
    // Do not restore filtering from form state retained before the controls were hidden.
    filter_formula_noise: false,
    force_retranscribe: Boolean(form.forceRetranscribe),
    ...(form.qwenMode ? { qwen_options: {
      mode: form.qwenMode,
      batch_size: form.qwenBatchSize ?? 2,
      chunk_seconds: form.qwenChunkSeconds ?? 90,
      align_batch_size: form.qwenAlignBatchSize ?? 2,
      load_profile: form.qwenLowMemory ? "low-memory" : "standard",
      auto_recovery: form.qwenMode !== "manual" || Boolean(form.qwenAutoRecovery),
    } } : {}),
  };
}
