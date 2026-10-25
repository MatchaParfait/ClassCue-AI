import type { QuestionRun } from "../api/questions";
import { formatTime } from "../utils";

export const QUESTION_STATUSES: Record<QuestionRun["status"], string> = {
  queued: "排队中", analyzing: "正在分析", completed: "已完成", failed: "分析失败", interrupted: "已停止",
};
const DEPTH = { concept: "概念解释", method: "方法讲解", example: "例题应用" };

export function QuestionUnderstanding({ run, currentId, dirty }: { run?: QuestionRun; currentId?: string; dirty: boolean }) {
  if (!run) return <p className="bili-muted">尚未分析题目。</p>;
  const analysis = run.analysis;
  const input = run.input;
  const sourceLabel = (source: string) => {
    const labels: Record<string, string> = { "marker.title": "考点标题", "marker.note": "考点说明",
      "marker.source_quote": "原字幕依据", "context.course": "课程", "context.notes": "学习要求", "question.text": "题目文字" };
    if (source.startsWith("slide:")) {
      const slide = input?.question.images.find(image => image.source === source);
      return slide ? `PPT #${slide.slide_id} · 截图 ${formatTime(slide.representative_ms)}` : `PPT #${source.slice(6)}`;
    }
    if (source.startsWith("capture:")) {
      const capture = input?.question.images.find(image => image.source === source);
      return capture ? `视频截图 · ${formatTime(capture.captured_at_ms ?? capture.representative_ms)}` : "视频截图（已删除）";
    }
    if (source.startsWith("classroom:")) {
      const segment = input?.classroom.classroom_segments.find(item => String(item.id) === source.slice(10));
      return segment ? `课堂字幕 ${formatTime(segment.start_ms)}：${segment.text}` : source;
    }
    return labels[source] ?? source;
  };
  const basis = (ids: string[]) => ids.map(id => {
    const found = [...(analysis?.facts ?? []), ...(analysis?.knowledge_points ?? []), ...(analysis?.methods ?? [])].find(item => item.id === id);
    return found ? `${id}：${found.text}` : id;
  }).join("；");
  return <>
    <div className="question-run-status"><strong>{QUESTION_STATUSES[run.status]}</strong>
      {run.id === currentId && !dirty ? <span className="bili-positive">当前搜索依据</span>
        : <span className="bili-muted">{["queued", "analyzing"].includes(run.status) ? "本次分析" : "历史记录"}</span>}
    </div>
    {(run.stale || dirty) && <p className="bili-warning" role="status">材料已变化，此分析不代表当前输入。请重新分析。</p>}
    {run.error && <p className="field-error" role="alert">{run.error}</p>}
    {analysis && <>
      <p className="question-kind">{({ exercise: "具体题目", topic: "知识点", unclear: "材料不足，无法确定目标" })[analysis.kind]}</p>
      {!!analysis.unknowns.length && <div className="bili-warning"><strong>待明确</strong><ul>{analysis.unknowns.map((text, i) => <li key={i}>{text}</li>)}</ul></div>}
      <div className="question-understanding-grid">
        <section><h4>材料中的事实</h4>{analysis.facts.length ? <dl className="question-facts">{analysis.facts.map(fact => <div key={fact.id}>
          <dt>{({ condition: "题目条件", task: "求解目标", topic: "主题" })[fact.kind]} <small>{fact.id}</small></dt>
          <dd>{fact.text}<details><summary>来源 · {fact.sources.length}</summary><ul>{fact.sources.map(source => <li key={source}>{sourceLabel(source)}</li>)}</ul></details></dd>
        </div>)}</dl> : <p className="bili-muted">无已确认事实。</p>}</section>
        <section><h4>知识点与解法推断</h4>{[...analysis.knowledge_points, ...analysis.methods].map(item => <div className="question-inference" key={item.id}>
          <span className="bili-muted">{analysis.methods.includes(item) ? "可能的解法" : "知识点"}</span><p>{item.text}</p>
          <details><summary>推断依据</summary><p>{basis(item.basis)}</p></details>
        </div>)}</section>
      </div>
      {!!analysis.criteria.length && <section className="question-criteria"><h4>字幕核验要求</h4><ol>{analysis.criteria.map(item => <li key={item.id}>
        <div><strong>{item.text}</strong><span className="bili-muted">{item.required ? "核心" : "补充"} · {DEPTH[item.depth]}</span></div>
        <p>所需证据：{item.evidence_needed}</p><details><summary>要求依据</summary><p>{basis(item.basis)}</p></details>
      </li>)}</ol></section>}
      {!!analysis.allowed_variations.length && <section><h4>类似题允许的差异</h4><ul>{analysis.allowed_variations.map((text, i) => <li key={i}>{text}</li>)}</ul></section>}
    </>}
    <details className="question-debug"><summary>运行详情</summary>
      <dl><dt>分析 ID</dt><dd>{run.id}</dd><dt>模型</dt><dd>{run.profile?.model || "未记录"}</dd>
        <dt>提示词版本</dt><dd>{run.version}</dd><dt>输入指纹</dt><dd>{run.input_hash}</dd></dl>
      {input && <><h4>本次输入快照</h4><p>课程：{input.classroom.context.course || "未填写"}</p>
        <p>学习要求：{input.classroom.context.notes || "未填写"}</p><p>题目文字：{input.question.text || "未填写"}</p>
        <p>考点：{input.classroom.marker.title}</p><blockquote>{input.classroom.marker.source_quote}</blockquote>
        <ul>{input.question.images.map(slide => <li key={slide.source}>{slide.capture_id ? "视频截图" : `PPT #${slide.slide_id}`} · {formatTime(slide.start_ms)} ~ {formatTime(slide.end_ms)} · 截图 {formatTime(slide.representative_ms)}</li>)}</ul>
        <details><summary>课堂上下文 · {input.classroom.classroom_segments.length} 条</summary>{input.classroom.classroom_segments.map(segment =>
          <p key={segment.id}>{formatTime(segment.start_ms)} ~ {formatTime(segment.end_ms)}　{segment.text}</p>)}</details></>}
      {analysis && <details><summary>结构化分析</summary><pre>{JSON.stringify(analysis, null, 2)}</pre></details>}
    </details>
  </>;
}
