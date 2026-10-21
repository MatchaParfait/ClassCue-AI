import { CircuitBoard, Cpu } from "lucide-react";
import type { AsrModel } from "../types";
import { GpuDeviceStatus, gpuUnavailableReason } from "./GpuDeviceStatus";

export function ParaformerDeviceControls({
  device, model, onChange, onSelect,
}: {
  device: "cpu" | "gpu";
  model: AsrModel;
  onChange: (value: "cpu" | "gpu") => void;
  onSelect?: (value: "cpu" | "gpu") => void;
}) {
  const gpuAvailable = model.gpu_available === true;
  const reason = gpuUnavailableReason(model);
  return (
    <div className="firered-device-controls">
      <fieldset className="firered-device-selector">
        <legend>处理设备</legend>
        <div className="firered-device-modes">
          <label className={`${device === "gpu" ? "selected " : ""}${gpuAvailable ? "" : "unavailable"}`}
            title={gpuAvailable ? model.gpu_name : reason}>
            <input type="radio" name={`${model.engine}-device`} value="gpu" checked={device === "gpu"}
              disabled={!gpuAvailable} onClick={() => onSelect?.("gpu")} onChange={() => onChange("gpu")} />
            <CircuitBoard size={16} aria-hidden="true" /><span>{gpuAvailable ? "GPU（推荐）" : "GPU"}</span>
          </label>
          <label className={device === "cpu" ? "selected" : ""}>
            <input type="radio" name={`${model.engine}-device`} value="cpu" checked={device === "cpu"}
              onClick={() => onSelect?.("cpu")} onChange={() => onChange("cpu")} />
            <Cpu size={16} aria-hidden="true" /><span>CPU</span>
          </label>
        </div>
      </fieldset>
      <GpuDeviceStatus model={model} />
    </div>
  );
}
