import { useEffect, useState } from "react";
import "./WarmAtmosphere.css";

// Soft, pre-feathered gradients give the background its blur without a canvas
// or a continuously recomputed blur filter.
export function WarmAtmosphere({ active }: { active: boolean }) {
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return <div className="warm-atmosphere" aria-hidden="true" data-moving={active && visible}>
    <span className="warm-atmosphere-lemon" />
    <span className="warm-atmosphere-honey" />
  </div>;
}
