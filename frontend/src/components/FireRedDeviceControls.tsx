import { AlertTriangle, CircuitBoard } from "lucide-react";
import type { AsrModel } from "../types";
import { GpuDeviceStatus } from "./GpuDeviceStatus";

export const FIRERED_CPU_NOTICE =
  "FireRed CPU 处理已禁用：推理过慢且内存需求较高。请明确选择 GPU 并保存，或改用支持 CPU 的 Paraformer / Dolphin。";

export function FireRedDeviceControls({
  device, onChange, onSelect, model,
}: {
  device: "gpu" | "cpu";
  onChange: (value: "gpu" | "cpu") => void;
  onSelect?: (value: "gpu") => void;
  model?: AsrModel;
}) {
  return (
    <div className="firered-device-controls">
      <fieldset className="firered-device-selector">
        <legend>处理设备</legend>
        <div className="firered-device-modes">
          <label className={device === "gpu" ? "selected" : ""}>
            <input type="radio" name="firered-device" value="gpu" checked={device === "gpu"}
              onClick={() => onSelect?.("gpu")} onChange={() => onChange("gpu")} />
            <CircuitBoard size={16} aria-hidden="true" /><span>GPU</span>
          </label>
        </div>
      </fieldset>
      {model && <GpuDeviceStatus model={model} />}
        {device === "cpu" && <p className="firered-cpu-notice" role="alert">
          <AlertTriangle size={16} aria-hidden="true" /><span>{FIRERED_CPU_NOTICE}</span>
        </p>}
    </div>
  );
}
