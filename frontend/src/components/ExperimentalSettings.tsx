import { FlaskConical, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { saveCyberspacePreference, useCyberspacePreference, ANALYSIS_VARIANTS, useAnalysisVariant, saveAnalysisVariant, type CyberspacePreference } from "../cyberspace/preferences";
import { CyberspaceDemo } from "./CyberspaceDemo";
import { getAiSettings } from "../api/client";

export function ExperimentalSettings({ active = true }: { active?: boolean }) {
  const preference = useCyberspacePreference();
  const analysisVariant = useAnalysisVariant();
  const [error, setError] = useState("");
  const [demo, setDemo] = useState(false);
  const [demoModel, setDemoModel] = useState<string | null>(null);
  useEffect(() => {
    if (!demo || analysisVariant !== "auto") return;
    let cancelled = false;
    setDemoModel(null);
    getAiSettings().then(settings => { if (!cancelled) setDemoModel(settings.model ?? ""); })
      .catch(() => { if (!cancelled) setDemoModel(""); });
    return () => { cancelled = true; };
  }, [demo, analysisVariant]);
  useEffect(() => { if (!active) setDemo(false); }, [active]);
  const save = (value: CyberspacePreference) => {
    setError(saveCyberspacePreference(value) ? "" : "无法保存显示设置，请检查浏览器存储权限。");
  };
  return <section className="settings-section experimental-settings">
    <div className="settings-section-heading"><FlaskConical size={20} /><h2>实验功能</h2></div>
    <label className="experiment-switch">
      <span><strong>字幕空间</strong><small>单视频处理可视化 · 实验性</small></span>
      <input type="checkbox" role="switch" aria-label="启用实验性字幕空间" checked={preference !== "off"}
        onChange={(event) => save(event.target.checked ? "dynamic" : "off")} />
    </label>
    <p className="settings-hint">默认关闭。</p>
    <fieldset className="analysis-quality-picker"><legend>动画主题</legend>
      {ANALYSIS_VARIANTS.map(variant => <label key={variant.id} className="analysis-quality-option" data-selected={analysisVariant === variant.id}>
        <input type="radio" name="analysis-variant" value={variant.id} checked={analysisVariant === variant.id}
          onChange={() => setError(saveAnalysisVariant(variant.id) ? "" : "无法保存显示设置，请检查浏览器存储权限。")} />
        <span><strong>{variant.name}</strong></span>
      </label>)}
    </fieldset>
    {analysisVariant === "auto" && <p className="settings-hint">按模型名称匹配，不区分大小写：DeepSeek → 三个大肥鱼版本之一；GPT → GPT；其他 → 黑洞或奶龙。同一任务保持选定主题。</p>}
    <p className="settings-hint">选择后自动保存。开启后显示完整字幕空间；语音转写的柔光背景与考点分析使用同一主题。</p>
    <div className="experiment-demo-entry"><button type="button" className="secondary-button" aria-haspopup="dialog" onClick={() => setDemo(true)}><Play size={16} />观看演示</button><span>示例字幕 · 无需导入视频</span></div>
    {error && <p className="field-error" role="alert">{error}</p>}
    {demo && active && analysisVariant === "auto" && demoModel === null && <p className="settings-hint" role="status">正在读取模型名称…</p>}
    {demo && active && (analysisVariant !== "auto" || demoModel !== null) && <CyberspaceDemo analysisQuality="high" analysisVariant={analysisVariant} aiModel={demoModel ?? undefined} onClose={() => { setDemo(false); setDemoModel(null); }} />}
  </section>;
}
