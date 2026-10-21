import { useQuery } from "@tanstack/react-query";
import { getAsrModelStatus } from "../api/client";
import type { AsrModel } from "../types";

export function asrStatusOptions(model: AsrModel | undefined, full = false) {
  return {
    queryKey: ["asr-status", model?.id, model?.device, full] as const,
    queryFn: () => getAsrModelStatus(model!.id, { full }),
    enabled: Boolean(model),
    staleTime: full ? 300_000 : 60_000,
    retry: false,
  };
}

export function useAsrStatus(model: AsrModel | undefined) {
  return useQuery(asrStatusOptions(model));
}
