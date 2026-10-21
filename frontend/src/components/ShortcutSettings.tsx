import { useUnsavedDraft } from "./UnsavedChanges";
import { useEffect, useState } from "react";
import { Keyboard, RotateCcw, Save, X } from "lucide-react";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { bindingFromEvent, defaultShortcuts, formatBinding, readShortcuts, saveShortcuts, shortcutDefinitions, validateShortcuts, type ShortcutMap } from "../shortcuts";
import "../shortcuts.css";

const nativeBinding = (value: string) => value.toUpperCase().replace("CTRL", "Ctrl").replace("ALT", "Alt").replace("SHIFT", "Shift");

function KeyRecorder({ label, value, disabled, onChange, onError }: {
  label: string; value: string; disabled?: boolean; onChange: (value: string) => void; onError: (message: string) => void;
}) {
  const [recording, setRecording] = useState(false);
  return <div className={`shortcut-recorder${recording ? " is-recording" : ""}`}>
    <input data-shortcut-recorder readOnly disabled={disabled} aria-label={label} value={formatBinding(value)}
      placeholder={recording ? "请按下快捷键…" : label.includes("备用") ? "备用键（可选）" : "未设置"} title="点击后按下快捷键；Backspace 清除，Esc 结束录入"
      onFocus={() => setRecording(true)} onBlur={() => setRecording(false)}
      onKeyDown={event => {
        if (event.key === "Tab") return;
        event.preventDefault(); event.stopPropagation();
        if (event.key === "Escape") { event.currentTarget.blur(); return; }
        if (event.nativeEvent.isComposing || event.repeat) return;
        if (["Backspace", "Delete"].includes(event.key) && !event.ctrlKey && !event.altKey && !event.shiftKey && !event.metaKey) { onChange(""); return; }
        try { const value = bindingFromEvent(event.nativeEvent); if (value) { onChange(value); onError(""); } }
        catch { onError("请选择字母、数字、方向键或功能键；Tab、Enter、Esc 保留基础操作。"); }
      }} />
    <button type="button" className="icon-button" aria-label={`清除${label}`} title="清除快捷键" disabled={disabled || !value} onClick={() => onChange("")}><X size={14} /></button>
  </div>;
}

export function ShortcutSettings() {
  const api = useDesktopAPI();
  const [draft, setDraft] = useState<ShortcutMap>(() => readShortcuts());
  const [wake, setWake] = useState("");
  const [nativeReady, setNativeReady] = useState(false);
  const [loadingVersion, setLoadingVersion] = useState(0);
  const [saving, setSaving] = useState(false);
  const [savedDraft, setSavedDraft] = useState<ShortcutMap>(() => readShortcuts());
  const [savedWake, setSavedWake] = useState("");
  const dirty = JSON.stringify(draft) !== JSON.stringify(savedDraft) || Boolean(api && wake !== savedWake);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!api) return;
    let disposed = false;
    setNativeReady(false);
    void api.describe().then(state => {
      if (!disposed) { setWake(nativeBinding(state.hotkey)); setSavedWake(nativeBinding(state.hotkey)); setNativeReady(true); setError(""); }
    }).catch(reason => { if (!disposed) setError(`无法读取桌面快捷键：${String(reason)}`); });
    return () => { disposed = true; };
  }, [api, loadingVersion]);
  const changed = () => { setNotice(""); setError(""); };
  const conflict = validateShortcuts(draft, api ? wake : "");
  const save = async (leaving = false) => {
    if (saving) throw new Error("快捷键正在保存，请稍候。");
    if (conflict) throw new Error(conflict);
    if (api && !nativeReady) throw new Error("请先重新读取桌面快捷键。");
    setSaving(true); setError(""); setNotice("");
    let previousWake = "", wakeChanged = false;
    try {
      if (api) {
        previousWake = nativeBinding((await api.describe()).hotkey);
        if (previousWake !== wake) { await api.set_hotkey(wake); wakeChanged = true; }
      }
      saveShortcuts(draft);
      setSavedDraft(draft); setSavedWake(wake); clearUnsaved(); setNotice("快捷键已保存并生效。");
      return true;
    } catch (reason) {
      let message = String(reason);
      if (api && wakeChanged) {
        try { await api.set_hotkey(previousWake); }
        catch { message += "；唤起快捷键未能恢复，请重新读取并检查设置。"; setNativeReady(false); }
      }
      setError(`保存失败：${message}`);
      if (leaving) throw new Error(message);
      return false;
    } finally { setSaving(false); }
  };
  const clearUnsaved = useUnsavedDraft({ label: "快捷键设置", dirty, busy: saving,
    save: () => save(true), discard: () => { setDraft(savedDraft); setWake(savedWake); setError(""); setNotice(""); },
  });
  return <section className="settings-section shortcut-settings" aria-label="快捷键设置">
    <div className="settings-section-heading"><div className="settings-section-title"><Keyboard size={20} /><h2>快捷键设置</h2></div></div>
    <p className="settings-hint">点击按键框后直接按下新组合键，保存后生效。每项应用内动作最多设置两个按键，清空可停用。输入文字、录入快捷键或弹窗操作时，不会触发复习和播放命令。</p>
    <div className="shortcut-settings-actions">
      <div className="shortcut-save-actions">
        <button className="primary-button" type="button" disabled={saving || Boolean(conflict) || Boolean(api && !nativeReady) || !dirty} onClick={() => void save()}><Save size={16} />{saving ? "正在保存…" : "保存快捷键"}</button>
        <button className="secondary-button" type="button" disabled={saving} onClick={() => { setDraft(defaultShortcuts()); if (api && nativeReady) setWake(""); changed(); }}><RotateCcw size={16} />恢复默认</button>
        {dirty && <span className="settings-hint">有未保存的更改</span>}
      </div>
      {(error || conflict) && <p role="alert" className="field-error shortcut-feedback">{error || conflict}</p>}
      {notice && <p role="status" className="shortcut-feedback">{notice}</p>}
    </div>
    <fieldset disabled={saving} className="shortcut-fields">
      <div className="shortcut-group"><h3>启动与唤起</h3>
        <div className="shortcut-row">
          <div><strong>启动 / 唤起应用</strong><p>应用未启动时启动；最小化或隐藏到托盘后恢复窗口。支持 Ctrl+Alt+字母/数字或 Ctrl+Shift+字母/数字，默认不启用。</p></div>
          <KeyRecorder label="启动或唤起快捷键" value={wake} disabled={!api || !nativeReady} onChange={value => { setWake(value); changed(); }} onError={setError} />
        </div>
        {!api && <p className="settings-hint">启动 / 唤起快捷键需在桌面应用中设置，下方应用内快捷键可正常修改。</p>}
        {api && !nativeReady && <button type="button" className="secondary-button compact" onClick={() => setLoadingVersion(value => value + 1)}>重新读取桌面快捷键</button>}
      </div>
      {["全局", "复习", "播放器"].map(group => <div className="shortcut-group" key={group}>
        <h3>{group}</h3>
        {shortcutDefinitions.filter(item => item.group === group).map(item => <div className="shortcut-row" key={item.id}>
          <div><strong>{item.label}</strong><p>{item.hint}</p></div>
          <div className="shortcut-bindings">{[0, 1].map(index => <KeyRecorder key={index} label={`${item.label}${index ? "备用" : ""}快捷键`}
            value={draft[item.id][index] ?? ""} onError={setError} onChange={value => {
              const bindings = [...draft[item.id]];
              bindings[index] = value;
              setDraft({ ...draft, [item.id]: bindings.filter(Boolean) }); changed();
            }} />)}</div>
        </div>)}
      </div>)}
      <div className="shortcut-standard"><strong>通用键盘操作（固定）</strong>
        <p>Tab / Shift+Tab 切换焦点；Enter 确认或提交；Esc 关闭菜单、取消弹窗或编辑；方向键选择搜索结果与菜单项。这些按键保持标准行为。</p>
        <p>系统和浏览器占用的组合键无法由应用接管。</p>
      </div>
    </fieldset>

  </section>;
}
