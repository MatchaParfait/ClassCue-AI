import { ArrowLeftRight } from "lucide-react";
import { useState } from "react";
import { saveAutoAlignSecondary, useAutoAlignSecondary } from "../secondaryViewPreferences";

export function SecondaryViewSettings() {
  const enabled = useAutoAlignSecondary();
  const [error, setError] = useState("");
  return <section className="settings-section" aria-label="副视角">
    <div className="settings-section-heading"><div className="settings-section-title"><ArrowLeftRight size={20} /><h2>副视角</h2></div></div>
    <label className="settings-switch">
      <span><strong>导入副视角后自动对齐</strong></span>
      <input type="checkbox" role="switch" aria-label="导入副视角后自动对齐" checked={enabled}
        onChange={event => setError(saveAutoAlignSecondary(event.target.checked) ? "" : "无法保存副视角设置，请检查浏览器存储权限。")} />
    </label>
    {error && <p className="field-error" role="alert">{error}</p>}
  </section>;
}
