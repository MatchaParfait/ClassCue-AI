import { useEffect, useState } from "react";

export function usePresence(active: boolean, duration = 180) {
  const [present, setPresent] = useState(active);
  useEffect(() => {
    if (active) { setPresent(true); return; }
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { setPresent(false); return; }
    const timer = window.setTimeout(() => setPresent(false), duration);
    return () => window.clearTimeout(timer);
  }, [active, duration]);
  return active || present;
}
