import { Pause, Play, RotateCcw, Scan, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Marker, Subtitle } from "../types";
import type { EvidenceCue, SpaceSnapshot, TaskSpace } from "../cyberspace/runtime";
import { EvidenceFeedback } from "./EvidenceFeedback";
import { PhaseLabel } from "./PhaseLabel";
import { PreparationCaption } from "./PreparationCaption";
import { useModalFocus } from "../hooks/useModalFocus";
import { resolveAnalysisVariant, type AnalysisQuality, type AnalysisVariant } from "../cyberspace/preferences";

const STAGES = ["音频准备", "字幕形成", "AI 分析", "考点命中"];
const STARTS = [0, 5, 12, 18];
const END = 27;
const NO_SUBTITLES: Subtitle[] = [];
const ANALYSIS_RATE = 1 / 2.5;
type DemoData = { subtitles: Subtitle[]; markers: Marker[] };

export function CyberspaceDemo({ onClose, analysisQuality = "high", analysisVariant: variant = "nailong", aiModel }: { onClose(): void; analysisQuality?: AnalysisQuality; analysisVariant?: AnalysisVariant; aiModel?: string }) {
  const id = useId();
  const analysisVariant = resolveAnalysisVariant(variant, aiModel, `demo:${id}`);
  const dialog = useRef<HTMLDivElement>(null), close = useRef<HTMLButtonElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null), controller = useRef<TaskSpace | null>(null);
  const [data, setData] = useState<DemoData | null>(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [paused, setPaused] = useState(false);
  const [run, setRun] = useState(0);
  const [selected, setSelected] = useState<number | null>(null);
  const [evidenceCue, setEvidenceCue] = useState<EvidenceCue | null>(null);
  const [hidden, setHidden] = useState(() => document.hidden);
  const [reduced, setReduced] = useState(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);
  useModalFocus(true, dialog, close, onClose);

  useEffect(() => {
    const change = () => setHidden(document.hidden);
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const motion = () => setReduced(media?.matches ?? false);
    document.addEventListener("visibilitychange", change);
    media?.addEventListener("change", motion);
    return () => { document.removeEventListener("visibilitychange", change); media?.removeEventListener("change", motion); };
  }, []);

  const stage = elapsed < STARTS[1] ? 0 : elapsed < STARTS[2] ? 1 : elapsed < STARTS[3] ? 2 : 3;
  const count = stage < 3 ? 0 : reduced ? data?.markers.length ?? 0 : Math.min(data?.markers.length ?? 0, 2 + Math.floor((elapsed - STARTS[3]) / 3) * 2);
  const markers = useMemo(() => data?.markers.slice(0, count) ?? [], [data, count]);
  const waitingFirstText = stage === 0 && elapsed >= STARTS[1] - 1;
  const subtitleCount = stage === 1 && !reduced ? 24 + Math.floor((elapsed - STARTS[1]) / 2) * 12 : data?.subtitles.length ?? 0;
  const subtitles = useMemo(() => stage === 0 ? NO_SUBTITLES : data?.subtitles.slice(0, subtitleCount) ?? NO_SUBTITLES,
    [data, stage, subtitleCount]);
  const snapshot = useMemo<SpaceSnapshot>(() => ({ subtitles, markers,
    phase: elapsed >= END ? "completed" : stage === 0 && !waitingFirstText ? "preparing" : stage <= 1 ? "transcribing" : "analyzing", reduced, paused: paused || hidden,
  }), [subtitles, markers, stage, waitingFirstText, elapsed >= END, reduced, paused, hidden]);
  const latest = useRef(snapshot); latest.current = snapshot;

  useEffect(() => {
    let disposed = false, instance: TaskSpace | null = null;
    setReady(false); setError("");
    setEvidenceCue(null);
    // The preview imports neither processing APIs nor job components.
    Promise.all([import("../cyberspace/runtime.js"), import("../../../experiments/subtitle-cyberspace/src/fixture.js")])
      .then(([{ createTaskSpace }, { fixture }]) => {
        if (disposed || !canvas.current) return;
        setData({ subtitles: fixture.subtitles, markers: fixture.markers.map(marker => ({ ...marker,
          video_id: 0, analysis_mode: "medium_recall", review_status: "active", importance: "standard",
        })) });
        instance = createTaskSpace(canvas.current, { key: `settings-demo:${id}:${run}`,
          analysisQuality, analysisVariant, analysisPlaybackRate: ANALYSIS_RATE,
          onSelect: setSelected, onError: message => { if (!disposed) setError(message); }, onQuality: () => {},
          onEvidence: cue => { if (!disposed) setEvidenceCue(cue); },
          onReady: () => { if (!disposed) setReady(true); },
        });
        controller.current = instance;
        instance.update(latest.current);
      }).catch(() => { if (!disposed) setError("演示暂时无法加载，请重试。"); });
    return () => { disposed = true; instance?.dispose(); controller.current = null; };
  }, [id, run, analysisQuality, analysisVariant]);
  useEffect(() => { controller.current?.update(snapshot); }, [snapshot]);
  useEffect(() => { controller.current?.select(selected); }, [selected]);
  useEffect(() => {
    if (!ready || error || paused || hidden || reduced || elapsed >= END) return;
    const timer = window.setInterval(() => setElapsed(value => Math.min(END,
      Math.round((value + .25 * (value < STARTS[2] ? 1 : ANALYSIS_RATE)) * 100) / 100)), 250);
    return () => window.clearInterval(timer);
  }, [ready, error, paused, hidden, reduced, elapsed >= END]);

  const restart = (time = 0) => {
    setElapsed(time); setPaused(false); setSelected(null); setRun(value => value + 1);
  };
  const detail = markers.find(marker => marker.id === selected);
  return createPortal(<div className="subtitle-demo-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={dialog} className="subtitle-demo-dialog" role="dialog" aria-modal="true" aria-labelledby={`${id}-title`}>
      <header className="subtitle-demo-heading"><div><h2 id={`${id}-title`}>字幕空间演示</h2><span>示例数据 · 不调用 ASR / AI</span></div>
        <div className="subtitle-demo-tools">
          <button type="button" aria-label={paused ? "继续演示" : "暂停演示"} title={paused ? "继续演示" : "暂停演示"} disabled={!ready || Boolean(error) || reduced || elapsed >= END} onClick={() => setPaused(value => !value)}>{paused ? <Play size={17} /> : <Pause size={17} />}</button>
          <button type="button" aria-label="重播演示" title="重播演示" onClick={() => restart()}><RotateCcw size={17} /></button>
          <button type="button" aria-label="重置演示视角" title="重置演示视角" disabled={!ready || Boolean(error)} onClick={() => controller.current?.resetView()}><Scan size={17} /></button>
          <button ref={close} type="button" aria-label="关闭演示" title="关闭演示" onClick={onClose}><X size={19} /></button>
        </div>
      </header>
      <div className="subtitle-demo-scene" data-stage={stage} data-analysis-variant={analysisQuality === "high" && stage > 1 ? analysisVariant : undefined}>
        <canvas ref={canvas} aria-label="字幕空间演示场景" />
        <div className="subtitle-demo-label"><span>SUBTITLE SPACE / DEMO</span><h3 aria-label={STAGES[stage]}><PhaseLabel text={STAGES[stage]} /></h3></div>
        {!ready && !error && <p className="subtitle-demo-loading" role="status">正在加载演示…</p>}
        {error && <div className="subtitle-demo-loading" role="alert"><p>{error}</p><button type="button" onClick={() => restart()}>重新加载</button></div>}
        {stage === 0 && <PreparationCaption waitingFirstText={waitingFirstText} />}
        {stage === 1 && data && <blockquote className="subtitle-demo-quote">{data.subtitles[Math.min(8, Math.floor(elapsed - STARTS[1]))]?.text}</blockquote>}
        {stage === 3 && <aside className="subtitle-demo-results" aria-label="示例考点">
          <h3>示例考点 <span>{markers.length}</span></h3>
          {markers.map(marker => <button type="button" key={marker.id} aria-pressed={selected === marker.id} onClick={() => { controller.current?.select(marker.id, { travel: true }); setSelected(marker.id); }}><EvidenceFeedback cue={evidenceCue} id={marker.id} />{marker.title}</button>)}
          {detail && <div className="subtitle-demo-detail"><blockquote>{detail.source_quote}</blockquote><p>{detail.note}</p></div>}
        </aside>}
      </div>
      <footer className="subtitle-demo-footer"><div role="group" aria-label="演示阶段">{STAGES.map((name, index) =>
        <button type="button" key={name} aria-pressed={stage === index} onClick={() => restart(STARTS[index])}><span>0{index + 1}</span>{name}</button>)}</div>
        <span role="status">{reduced ? "已减少动态效果" : elapsed >= END ? "演示结束" : paused ? "已暂停" : "演示中"}</span>
      </footer>
    </div>
  </div>, document.body);
}
