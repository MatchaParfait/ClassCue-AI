import { MousePointer2 } from "lucide-react";
import { useState } from "react";
import { saveDragTextHints, useDragTextHints } from "../libraryPreferences";

export function InteractionSettings() {
  const enabled = useDragTextHints();
  const [error, setError] = useState("");
  return <section className="settings-section" aria-label="交互提示">
    <div className="settings-section-heading"><div className="settings-section-title"><MousePointer2 size={20} /><h2>交互提示</h2></div></div>
    <label className="settings-switch">
      <span><strong>拖拽文字提示</strong></span>
      <input type="checkbox" role="switch" aria-label="拖拽文字提示" checked={enabled}
        onChange={(event) => setError(saveDragTextHints(event.target.checked) ? "" : "无法保存提示设置，请检查浏览器存储权限。")} />
    </label>
    {error && <p className="field-error" role="alert">{error}</p>}
  </section>;
}
