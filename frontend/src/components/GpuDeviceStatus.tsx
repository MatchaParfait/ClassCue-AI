import type { AsrModel } from "../types";

export function gpuUnavailableReason(model: AsrModel): string {
  return model.gpu_unavailable_reason || (model.gpu_available == null
    ? "GPU 状态待检查" : model.gpu_runtime_ready === false
    ? "GPU 环境未准备" : model.gpu_prepare_available === false
    ? "未检测到可用的 NVIDIA 驱动或设备" : "CUDA 不可用");
}

export function GpuDeviceStatus({ model }: { model: AsrModel }) {
  const description = model.gpu_available === false
    ? gpuUnavailableReason(model)
    : model.gpu_name || (model.gpu_available === true ? "CUDA 可用" : gpuUnavailableReason(model));
  return <p className="settings-hint gpu-device-status" role="status">{description}</p>;
}
