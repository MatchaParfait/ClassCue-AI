import type { MarkerImportance } from "../types";

export function MarkerImportanceCounts({ counts, folder = false }: {
  counts?: Record<MarkerImportance, number>;
  folder?: boolean;
}) {
  return <span className={`video-marker-counts${folder ? " folder-marker-counts" : ""}`}
    aria-label={folder ? "文件夹考点标签总数（含子文件夹）" : "考点标签数量"}>
    {([
      ["standard", "标准", "白色"],
      ["important", "重要", "蓝色"],
      ["critical", "很重要", "红色"],
    ] as const).map(([level, label, color]) => (
      <span className="video-marker-count" key={level}
        aria-label={`${color}${label}：${counts?.[level] ?? 0} 个`}
        title={`${label}（${color}）：${counts?.[level] ?? 0} 个${folder ? "，包含所有子文件夹" : ""}`}>
        <span className={`video-marker-swatch importance-${level}`} aria-hidden="true" />
        <span>{counts?.[level] ?? 0}</span>
      </span>
    ))}
  </span>;
}
