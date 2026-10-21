import { useRef } from "react";
import { usePointerGaze } from "./usePointerGaze";

/** Decorative, code-native motifs. Hidden in the preserved square theme. */
export function CharacterEye({ heading = false }: { heading?: boolean }) {
  const eyeRef = useRef<HTMLElement>(null);
  const pupilRef = useRef<HTMLSpanElement>(null);
  usePointerGaze(eyeRef, (x, y) => {
    if (!eyeRef.current || !pupilRef.current) return;
    const travel = Math.min(eyeRef.current.clientWidth, eyeRef.current.clientHeight) * .15;
    pupilRef.current.style.transform = `translate(${x * travel}px, ${y * travel}px)`;
  });
  return <span className={`character-eye${heading ? " character-eye-heading" : ""}`} aria-hidden="true">
    <i ref={eyeRef}><span className="character-eye-pupil" ref={pupilRef} /></i>
  </span>;
}

export function CharacterAccent() {
  const eyeRef = useRef<SVGCircleElement>(null);
  const pupilRef = useRef<SVGCircleElement>(null);
  const farPupilRef = useRef<SVGEllipseElement>(null);
  const silhouette = "M20 310C75 248 84 197 27 139C38 125 35 104 45 76C75 18 135 16 164 23C219 27 249 66 271 122C291 173 303 193 327 228C353 267 369 292 380 310Z";

  usePointerGaze(eyeRef, (dx, dy) => {
    const pupil = pupilRef.current, farPupil = farPupilRef.current;
    if (!pupil || !farPupil) return;
    const x = dx * 10, y = dy * 10;
    pupil.style.transform = `translate(${x}px, ${y}px)`;
    // Counter the far eye's tilt to keep both pupils moving in the same direction.
    const angle = Math.PI / 12;
    farPupil.style.transform = `translate(${(x * Math.cos(angle) + y * Math.sin(angle)) * .15}px, ${(-x * Math.sin(angle) + y * Math.cos(angle)) * .15}px)`;
  });

  return <div className="character-art" aria-hidden="true">
    <div className="character-art-fallback">
      <svg className="character-profile" viewBox="0 0 400 310" fill="none" focusable="false" aria-hidden="true">
        {/* The smaller, far eye sits behind the face and shares the near gaze. */}
        <g className="character-profile-far-eye" transform="rotate(15 39 95)">
          <ellipse cx="39" cy="95" rx="9" ry="15" fill="var(--character-profile-iris)" />
          <ellipse className="character-profile-far-pupil" ref={farPupilRef} cx="36" cy="96" rx="3.5" ry="7" fill="var(--character-ink)" />
        </g>
        <path className="character-profile-outline" d={silhouette} fill="var(--character-profile-fill)" />
        <path d="M31 143C51 144 73 144 94 143" stroke="var(--character-ink)" strokeWidth="2.8" strokeLinecap="round" />
        <circle className="character-profile-eye" ref={eyeRef} cx="117" cy="82" r="31" fill="var(--character-profile-iris)" stroke="var(--character-profile-eye-rim)" strokeWidth="2" />
        <circle className="character-profile-pupil" ref={pupilRef} cx="117" cy="82" r="14" fill="var(--character-ink)" />
      </svg>
    </div>
  </div>;
}
