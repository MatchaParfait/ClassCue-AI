import { useEffect, useState } from "react";
import { Monitor } from "lucide-react";
import { useDesktopAPI, type CloseBehavior } from "../hooks/useDesktopAPI";

export function DesktopSettings({ active = true }: { active?: boolean }) {
  const api = useDesktopAPI();
  const [behavior, setBehavior] = useState<CloseBehavior>("ask");
  const [tray, setTray] = useState(false);
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!api || !active) return;
    let disposed = false;
    setReady(false);
    void api.describe().then(value => {
      if (disposed) return;
      setBehavior(value.close_behavior ?? "ask");
      setTray(value.tray); setReady(true);
    }).catch(reason => { if (!disposed) setError(String(reason)); });
    return () => { disposed = true; };
  }, [api, active]);
  const save = async (work: () => Promise<unknown>, message: string) => {
    setSaving(true); setError(""); setNotice("");
    try { await work(); setNotice(message); }
    catch (reason) { setError(String(reason)); }
    finally { setSaving(false); }
  };
  return <section className="settings-section">
    <div className="settings-section-heading"><div className="settings-section-title"><Monitor size={20} /><h2>桌面设置</h2></div></div>
    {!api ? <p className="settings-hint">请在桌面应用中设置关闭窗口行为。</p> : <>
      <fieldset disabled={!ready || saving} className="desktop-settings-fields">
        <label className="settings-field"><span>点击右上角关闭按钮时</span>
          <select aria-label="关闭窗口行为" value={behavior} onChange={event => {
            const value = event.target.value as CloseBehavior;
            void save(async () => {
              const result = await api.set_close_behavior(value); setBehavior(result.close_behavior);
            }, "关闭窗口行为已保存。");
          }}>
            <option value="ask">每次询问</option>
            <option value="background" disabled={!tray}>最小化到托盘，后台继续</option>
            <option value="exit">退出应用</option>
          </select>
        </label>
        <p className="settings-hint">可在这里修改“下次不再询问”记住的选择。有处理任务时，退出仍需确认停止任务。{!tray && "当前托盘不可用，无法隐藏窗口。"}</p>
      </fieldset>
    </>}
    {error && <p className="field-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
