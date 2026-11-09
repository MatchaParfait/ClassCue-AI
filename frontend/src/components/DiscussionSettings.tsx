import { useId, useState } from "react";
import { MessageSquare } from "lucide-react";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { setDiscussionProvider, useDiscussionProvider } from "../hooks/useDiscussionProvider";
import { CodexConnectionSettings } from "./CodexDiscussion";
import { DshConnectionSettings } from "./DshConnectionSettings";
import "./DshQuestionActions.css";

export function DiscussionSettingsSection({ active = true, scope = "default" }: { active?: boolean; scope?: string }) {
  const desktop = useDesktopAPI();
  const provider = useDiscussionProvider();
  const name = useId();
  const [busy, setBusy] = useState(false);
  if (!desktop) return null;
  return <section className="settings-section discussion-settings-section" aria-labelledby={`${name}-title`}>
    <div className="settings-section-heading"><div className="settings-section-title">
      <MessageSquare size={20} /><h2 id={`${name}-title`}>讨论工具</h2>
    </div></div>
    <fieldset className="discussion-provider-selector settings-choice-selector" disabled={busy || !active}>
      <legend>默认讨论工具</legend>
      <div className="discussion-provider-options settings-choice-options">
        <label><input type="radio" name={name} value="dsh" checked={provider === "dsh"}
          onChange={() => setDiscussionProvider("dsh")} />DSH</label>
        <label><input type="radio" name={name} value="codex" checked={provider === "codex"}
          onChange={() => setDiscussionProvider("codex")} />Codex</label>
      </div>
    </fieldset>
    <div role="region" aria-label={`${provider === "dsh" ? "DSH" : "Codex"} 设置`}>
      {provider === "dsh" ? <DshConnectionSettings active={active} scope={scope} onBusyChange={setBusy} />
        : <CodexConnectionSettings active={active} scope={scope} onBusyChange={setBusy} />}
    </div>
  </section>;
}
