import { useState } from "react";

export type OnboardingMascotPose = "welcome" | "point" | "reading" | "complete";
type Direction = "left" | "right" | null;

export function OnboardingMascot({ pose, direction }: { pose: OnboardingMascotPose; direction: Direction }) {
  const asset = pose === "point" ? direction ? `point-${direction}` : "welcome" : pose;
  const src = `/art/onboarding/${asset === "point-left" ? "point-right" : asset}.png`;
  const [failed, setFailed] = useState("");
  return <div className="onboarding-mascot" aria-hidden="true" data-pose={asset}>
    <img key={src} src={src} alt="" width={256} height={256} draggable={false}
      hidden={failed === src} onError={() => setFailed(src)}
      style={{ transform: pose === "point" && direction === "left" ? "scaleX(-1)" : undefined }} />
  </div>;
}
