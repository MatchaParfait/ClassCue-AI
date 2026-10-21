import type { EvidenceCue } from "../cyberspace/runtime";
import "./EvidenceFeedback.css";

// Remount only the decoration for each explicit event. Button identity, focus,
// selection and open details remain intact, including repeated manual clicks.
export function EvidenceFeedback({ cue, id }: { cue: EvidenceCue | null; id: number }) {
  return cue?.id === id ? <span key={`${cue.serial}:${cue.phase}`} className="evidence-feedback"
    data-evidence-phase={cue.phase} aria-hidden="true" /> : null;
}
