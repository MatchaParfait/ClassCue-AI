import { Eye, EyeOff, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { revealAiApiKey } from "../api/client";

interface SavedApiKeyProps {
  connectionId?: string;
  baseUrl: string;
  hasApiKey: boolean;
  onError: (error: unknown) => void;
  editable?: boolean;
  draftValue?: string;
  onDraftChange?: (value: string) => void;
}

export function SavedApiKey({
  connectionId,
  baseUrl,
  hasApiKey,
  onError,
  editable = false,
  draftValue = "",
  onDraftChange,
}: SavedApiKeyProps) {
  const [revealedKey, setRevealedKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [draftVisible, setDraftVisible] = useState(false);
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => () => requestRef.current?.abort(), []);

  const toggleVisibility = async () => {
    if (editable && draftValue) {
      setDraftVisible((visible) => !visible);
      return;
    }
    if (revealedKey !== null || loading) {
      requestRef.current?.abort();
      requestRef.current = null;
      setRevealedKey(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    try {
      const key = connectionId
        ? await revealAiApiKey(baseUrl, controller.signal, connectionId)
        : await revealAiApiKey(baseUrl, controller.signal);
      if (!controller.signal.aborted) setRevealedKey(key);
    } catch (error) {
      if (!controller.signal.aborted) onError(error);
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        setLoading(false);
      }
    }
  };

  const visible = revealedKey !== null;
  const inputValue = editable
    ? draftValue || (visible ? revealedKey : hasApiKey ? "********" : "")
    : (visible ? revealedKey : hasApiKey ? "********" : "");
  const showingInput = editable ? (draftValue ? draftVisible : visible) : visible;
  const buttonLabel = loading || showingInput ? "隐藏 API Key" : "显示 API Key";
  return (
    <div className="ai-saved-key">
      <input
        type={showingInput ? "text" : "password"}
        aria-label={editable ? "API Key" : "已保存的 API Key"}
        readOnly={!editable}
        value={inputValue}
        onFocus={(event) => {
          if (editable && !draftValue && hasApiKey) event.currentTarget.select();
        }}
        onChange={(event) => {
          if (editable) {
            setRevealedKey(null);
            onDraftChange?.(event.target.value);
          }
        }}
        placeholder={editable ? "输入或粘贴 API Key" : "尚未保存 API Key"}
        autoComplete="off"
        spellCheck={false}
      />
      <button
        className="icon-button"
        type="button"
        title={buttonLabel}
        aria-label={buttonLabel}
        aria-pressed={showingInput}
        aria-busy={loading}
        disabled={!hasApiKey && !draftValue}
        onClick={() => void toggleVisibility()}
      >
        {loading ? <LoaderCircle className="spin" size={16} /> : visible ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}
