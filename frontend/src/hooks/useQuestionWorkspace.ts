import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { analyzeQuestion, getQuestionState, saveQuestionMaterial, type QuestionMaterial } from "../api/questions";
import { isTaskTerminal, useTaskStatus } from "./useTaskStatus";

const EMPTY: QuestionMaterial = { text: "", slide_ids: [] };
const ACTIVE = new Set(["queued", "analyzing"]);

export function useQuestionWorkspace(markerId: number) {
  const cache = useQueryClient();
  const state = useQuery({
    queryKey: ["question", markerId], queryFn: () => getQuestionState(markerId),
    refetchInterval: query => query.state.data?.runs.some(run => ACTIVE.has(run.status)) ? 1000 : false,
    refetchIntervalInBackground: false,
  });
  const [material, setMaterial] = useState<QuestionMaterial>(EMPTY);
  const [saved, setSaved] = useState<QuestionMaterial>(EMPTY);
  const initialized = useRef(false);
  const [selectedId, setSelectedId] = useState("");
  const [submittedJob, setSubmittedJob] = useState<string | null>(null);
  const runs = state.data?.runs ?? [];
  const run = runs.find(item => item.id === selectedId) ?? runs[0];
  const current = runs.find(item => item.status === "completed" && !item.stale);
  // History browsing must never change which job is polled or cancelled.
  const active = runs.find(item => ACTIVE.has(item.status));
  const jobId = active?.job_id ?? submittedJob;
  const task = useTaskStatus(jobId);
  const busy = Boolean(jobId && (!task.data || !isTaskTerminal(task.data)));
  const dirty = JSON.stringify(material) !== JSON.stringify(saved);
  const hasMaterial = Boolean(material.text.trim() || material.slide_ids.length || material.capture_ids?.length);
  const needsAnalysis = Boolean(hasMaterial || runs.length);

  useEffect(() => {
    if (!state.data || initialized.current) return;
    initialized.current = true;
    setMaterial(state.data.material); setSaved(state.data.material);
  }, [state.data]);
  useEffect(() => {
    if (!jobId || !task.data || !isTaskTerminal(task.data)) return;
    void cache.invalidateQueries({ queryKey: ["question", markerId] });
    void cache.invalidateQueries({ queryKey: ["bilibili", markerId] });
  }, [jobId, task.data?.status, cache, markerId]);

  const save = async () => {
    if (!dirty) return;
    const value = await saveQuestionMaterial(markerId, material);
    setMaterial(value); setSaved(value);
    await cache.invalidateQueries({ queryKey: ["question", markerId] });
    await cache.invalidateQueries({ queryKey: ["bilibili", markerId] });
  };
  const analyze = async () => {
    const result = await analyzeQuestion(markerId);
    setSubmittedJob(result.job_id); setSelectedId(result.analysis_id);
    await cache.invalidateQueries({ queryKey: ["question", markerId] });
  };
  const removeCapture = (id: string) => {
    setMaterial(value => ({ ...value, capture_ids: (value.capture_ids ?? []).filter(item => item !== id) }));
    setSaved(value => ({ ...value, capture_ids: (value.capture_ids ?? []).filter(item => item !== id) }));
  };
  return { state, material, setMaterial, dirty, save, analyze, busy, task, jobId, run, current,
    selectedId, select: setSelectedId, needsAnalysis, hasMaterial, removeCapture,
    discard: () => setMaterial(saved),
  };
}
