export function PreparationCaption({ waitingFirstText = false }: { waitingFirstText?: boolean }) {
  return <p className="subtitle-demo-quote subtitle-preparation-caption" role="status">
    {waitingFirstText ? "音频已就绪，正在识别第一段字幕" : "先静一会儿，正在准备课堂声音。"}
  </p>;
}
