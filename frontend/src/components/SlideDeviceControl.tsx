import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import { getSlideAcceleration, getSlideSelection } from "../api/slides";

export function SlideDeviceControl({ active, busy, onStateChange, showSettingsLink = true }: {
  active: boolean; busy: boolean;
  showSettingsLink?: boolean;
  onStateChange: (state: { blocked: boolean; preparing: boolean }) => void;
}) {
  const selection = useQuery({ queryKey: ["slides", "selection"], queryFn: getSlideSelection,
    enabled: active, retry: false });
  const value = selection.data?.backend ?? "auto";
  const status = useQuery({ queryKey: ["slides", "acceleration"], queryFn: () => getSlideAcceleration(),
    enabled: active && value === "cuda", staleTime: 30_000, retry: false });
  const blocked = !selection.data || selection.isError
    || Boolean(value === "cuda" && (!status.data?.ready || status.isError));
  useEffect(() => {
    onStateChange({ blocked, preparing: false });
  }, [onStateChange, blocked]);
  return <div className="slide-device-control">
    <div className="slide-extraction-actions">
      <span className="slide-roi-note">默认设备：{selection.isPending ? "正在读取…"
        : value === "cuda" ? "GPU（CUDA）" : value === "cpu" ? "CPU" : "自动"}</span>
      {showSettingsLink && <Link className="text-button" to="/settings?section=slides"><Settings2 size={14} />PPT 设置</Link>}
    </div>
    {value === "cuda" && status.data && !status.data.ready && <p className="field-error" role="alert">
      {status.data.gpu_unavailable_reason} 请在设置中准备 GPU 或切换设备。
    </p>}
    {selection.isError && <p className="field-error" role="alert">设备选择读取失败。
      <button type="button" className="text-button" disabled={busy} onClick={() => void selection.refetch()}>重新读取设备选择</button>
    </p>}
    {value === "cuda" && status.isError && <p className="field-error" role="alert">GPU 状态读取失败。
      <button type="button" className="text-button" disabled={busy} onClick={() => void status.refetch()}>重新读取设备状态</button>
    </p>}
  </div>;
}
