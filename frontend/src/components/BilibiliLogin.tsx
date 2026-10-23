import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, LoaderCircle, LogOut, QrCode, RotateCcw, X } from "lucide-react";
import { cancelBiliLogin, deleteBiliAccount, pollBiliLogin, startBiliLogin } from "../api/bilibili";
import type { BiliLoginSession } from "../api/bilibili";

const WAITING = new Set(["waiting_scan", "waiting_confirm"]);

export function BilibiliLogin({ configured, disabled, onActiveChange }: {
  configured: boolean; disabled: boolean; onActiveChange: (active: boolean) => void;
}) {
  const cache = useQueryClient();
  const [session, setSession] = useState<BiliLoginSession | null>(null);
  const [starting, setStarting] = useState(false), [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);
  const current = useRef<BiliLoginSession | null>(null);
  const generation = useRef(0), operation = useRef<AbortController | null>(null);
  const operationLock = useRef(false);
  const active = Boolean(session && WAITING.has(session.status));
  const updateSession = (value: BiliLoginSession | null) => {
    current.current = value;
    setSession(value);
  };

  useEffect(() => {
    onActiveChange(starting || active);
  }, [starting, active, onActiveChange]);
  useEffect(() => () => {
    generation.current += 1;
    operation.current?.abort();
    if (current.current && WAITING.has(current.current.status)) {
      void cancelBiliLogin(current.current.session_id).catch(() => undefined);
    }
    onActiveChange(false);
  }, [onActiveChange]);
  useEffect(() => {
    if (!session || !active) return;
    const expires = session.expires_at;
    const tick = () => {
      const left = Math.max(0, Math.ceil(expires - Date.now() / 1000));
      setSeconds(left);
      if (!left) {
        updateSession({ ...session, status: "expired", image: "" });
        void cancelBiliLogin(session.session_id).catch(() => undefined);
      }
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [session?.session_id, session?.expires_at, active]);
  useEffect(() => {
    if (!session || !active || disabled || error) return;
    let stopped = false;
    const version = generation.current;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const value = await pollBiliLogin(session.session_id, controller.signal);
        if (stopped || version !== generation.current) return;
        updateSession(value);
        if (value.status === "success") {
          await cache.invalidateQueries({ queryKey: ["bilibili-account"] });
        }
      } catch (failure) {
        if (stopped || version !== generation.current) return;
        updateSession({ ...session, status: "failed", image: "" });
        setError(failure instanceof Error ? failure.message : "扫码登录失败，请重试");
      }
    }, Math.max(3000, Math.min(15000, session.poll_after_ms)));
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [session, active, disabled, error, cache]);

  const start = async () => {
    if (operationLock.current || disabled) return;
    operationLock.current = true;
    const version = ++generation.current;
    const controller = new AbortController();
    operation.current = controller;
    setStarting(true); setError(""); updateSession(null);
    try {
      const value = await startBiliLogin(controller.signal);
      if (version !== generation.current) {
        void cancelBiliLogin(value.session_id).catch(() => undefined);
        return;
      }
      updateSession(value);
    } catch (failure) {
      if (version === generation.current) {
        setError(failure instanceof Error ? failure.message : "无法生成登录二维码，请重试");
      }
    } finally {
      if (version === generation.current) {
        operationLock.current = false;
        setStarting(false);
      }
    }
  };
  const cancel = () => {
    generation.current += 1;
    operationLock.current = false;
    operation.current?.abort();
    const previous = current.current;
    updateSession(null); setStarting(false); setError("");
    if (previous) void cancelBiliLogin(previous.session_id).catch(() => undefined);
  };
  const logout = async () => {
    if (operationLock.current || active || disabled) return;
    operationLock.current = true;
    setStarting(true); setError("");
    try {
      await deleteBiliAccount();
      updateSession(null);
      await cache.invalidateQueries({ queryKey: ["bilibili-account"] });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "退出登录失败，请重试");
    } finally {
      operationLock.current = false;
      setStarting(false);
    }
  };

  return <div className="bili-login">
    <p className="bili-muted">{configured ? "已保存登录状态" : "当前匿名读取"}</p>
    {active && session?.image && <div className="bili-login-qr">
      <img src={session.image} width={224} height={224} alt="B 站登录二维码" />
    </div>}
    <div role="status" aria-live="polite" className="bili-login-status">
      {starting ? <><LoaderCircle size={16} className="spin" />正在处理登录</>
        : session?.status === "waiting_confirm" ? "已扫码，请在手机上确认登录"
          : active ? "等待 B 站 App 扫码"
            : session?.status === "success" ? <><CheckCircle2 size={16} />登录成功，已在本机加密保存</>
              : session?.status === "expired" ? "二维码已过期" : null}
    </div>
    {active && <p className="bili-muted">{seconds} 秒后过期{disabled ? " · 当前任务结束后继续检查" : ""}</p>}
    {error && <p className="field-error" role="alert">{error}</p>}
    <div className="bili-actions">
      {!active && !starting && <button className="secondary-button compact" disabled={disabled} onClick={() => void start()}>
        {session?.status === "expired" || error ? <RotateCcw size={15} /> : <QrCode size={15} />}
        {session?.status === "expired" ? "刷新二维码" : error ? "重试登录" : configured ? "重新扫码登录" : "扫码登录"}
      </button>}
      {(active || starting) && <button className="text-button" onClick={cancel}><X size={15} />取消登录</button>}
      {configured && <button className="text-button" disabled={disabled || starting || active} onClick={() => void logout()}>
        <LogOut size={15} />退出登录
      </button>}
    </div>
  </div>;
}
