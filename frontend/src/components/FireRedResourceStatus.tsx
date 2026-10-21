import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Cpu, LoaderCircle } from "lucide-react";
import { getAsrResourceProfile } from "../api/client";
import type { FireRedResource } from "../types";

export function FireRedResourceProfile({ modelId, active = true }: { modelId: string; active?: boolean }) {
  const profile = useQuery({
    queryKey: ["settings", "asr", "resource-profile", modelId],
    queryFn: () => getAsrResourceProfile(modelId),
    enabled: active,
    staleTime: 30_000,
    retry: false,
  });
  return <div className="field-group firered-resource-profile">
    <span className="field-label"><Cpu size={15} aria-hidden="true" /> FireRed 自动资源档位</span>
    {profile.isPending && <span className="refresh-note"><LoaderCircle size={14} className="spin" /> 检测 CUDA 设备...</span>}
    {profile.error && <p className="field-error"><AlertCircle size={14} />{profile.error.message}</p>}
    {profile.data && <>
      <div className="video-info-meta">
        <span>原生显存 {profile.data.gpu_total_mib == null ? "未知" : `${(profile.data.gpu_total_mib / 1024).toFixed(1)} GB`}</span>
        <span>初始 BS{profile.data.initial_batch_size}</span>
        <span>{profile.data.cuda_available === false ? "CUDA 不可用" : profile.data.device}</span>
      </div>
      {profile.data.warning && <p className="field-error"><AlertCircle size={14} />{profile.data.warning}</p>}
    </>}
  </div>;
}

export function FireRedResourceStatus({ resource }: { resource?: FireRedResource | null }) {
  if (!resource || resource.engine !== "firered") return null;
  return <div className="firered-resource-status" aria-label="FireRed 运行资源">
    <div className="video-info-meta">
      <span>初始 BS{resource.initial_batch_size}</span>
      <strong>当前 BS{resource.current_batch_size ?? resource.initial_batch_size}</strong>
      <span>已保存 {resource.completed_segments ?? 0}/{resource.total_segments ?? 0} 个片段</span>
      {Boolean(resource.recovery_count) && <span>恢复 {resource.recovery_count} 次</span>}
      {resource.cache_hit && <span>字幕缓存命中</span>}
    </div>
    {resource.recovery_reason && <p className="job-error"><AlertCircle size={14} />{resource.recovery_reason}</p>}
  </div>;
}
