import { AlertCircle, Check, LoaderCircle } from "lucide-react";
import { useRef, useState } from "react";
import type { Marker, MarkerImportance } from "../types";

const LEVELS = [
  { value: "standard", label: "标准", color: "白色" },
  { value: "important", label: "重要", color: "蓝色" },
  { value: "critical", label: "很重要", color: "红色" },
] as const;

export function MarkerImportanceControl({ marker, onChange }: {
  marker: Marker;
  onChange: (marker: Marker, importance: MarkerImportance) => Promise<void>;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  const savingRef = useRef(false);
  const value = marker.importance ?? "standard";
  const save = async (importance: MarkerImportance) => {
    if (savingRef.current || importance === value) return;
    savingRef.current = true;
    setSaving(true);
    setError(false);
    try {
      await onChange(marker, importance);
    } catch {
      setError(true);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return <div className="marker-importance-control">
    <div className="marker-importance-row" role="group" aria-label={`重要程度：${marker.title}`} aria-busy={saving}>
      <span className={`marker-importance-label importance-${value}`}>
        {saving ? <LoaderCircle className="spin" size={13} aria-label="正在保存标签" /> : LEVELS.find((level) => level.value === value)?.label}
      </span>
      {LEVELS.map((level) => <button className={`marker-importance-swatch importance-${level.value}`} key={level.value}
        type="button" aria-label={`${level.label}（${level.color}）`} title={`${level.label}（${level.color}）`}
        aria-pressed={value === level.value} disabled={saving} onClick={() => void save(level.value)}>
        {value === level.value && <Check size={14} aria-hidden="true" />}
      </button>)}
    </div>
    {error && <p className="field-error" role="alert"><AlertCircle size={13} />标签保存失败，请重试</p>}
  </div>;
}
