import { useLayoutEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { ArrowLeft, Download, ExternalLink, MapPin } from "lucide-react";
import { getMaterialBlocks, materialAssetUrl, materialOriginalUrl, materialRegionUrl,
  type MaterialBlock, type MaterialRegion, type MaterialSource } from "../api/materials";
import "katex/dist/katex.min.css";

export function MaterialText({ text }: { text: string }) {
  return <div className="material-prose"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore" }]]} skipHtml
    components={{
      // Files never trigger network requests or script execution through imported markup.
      img: ({ alt }) => <span className="material-missing-image">[图片：{alt || "需要补充"}]</span>,
      a: ({ children }) => <span>{children}</span>,
    }}>{text}</ReactMarkdown></div>;
}

export function sourceLabel(block: MaterialBlock) {
  return block.page ? `第 ${block.page} 页` : block.line_start ? `第 ${block.line_start}–${block.line_end} 行`
    : `内容块 ${block.block_number ?? block.ordinal + 1}`;
}

export function MaterialRegions({ documentId, regions, onLocate }: {
  documentId: string; regions: MaterialRegion[]; onLocate?: (refs: MaterialSource[]) => void;
}) {
  return <>{regions.map((region, index) => <section className="material-region" key={`${region.id}:${index}`}>
    {onLocate && <button className="text-button" onClick={() => onLocate([region])}>
      <MapPin size={14} />{sourceLabel(region)}</button>}
    {region.image ? <img className="material-question-image" src={materialRegionUrl(documentId, region)}
      alt={`${sourceLabel(region)}题目原文`} />
      : <MaterialText text={region.text} />}
    {region.images?.map(image => <img key={image.image} className="material-question-image"
      src={materialAssetUrl(documentId, image.image)} alt="Word 内嵌题图" />)}
  </section>)}</>;
}

export function MaterialSourceView({ documentId, targets, onBack, onCreate, onAccount, onOpenOriginal }: {
  documentId: string; targets: MaterialSource[]; onBack: () => void;
  onCreate: (block: MaterialBlock) => void;
  onAccount: (block: MaterialBlock, state: string) => void;
  onOpenOriginal: () => void;
}) {
  const blocks = useQuery({ queryKey: ["materials", "blocks", documentId], queryFn: () => getMaterialBlocks(documentId) });
  const panel = useRef<HTMLDivElement>(null);
  function locate() {
    const target = panel.current?.querySelector<HTMLElement>(`[data-source="${targets[0]?.block_id ?? ""}"]`);
    if (target && panel.current) {
      const region = target.querySelector<HTMLElement>(".material-highlight") ?? target;
      panel.current.scrollTop = Math.max(0, panel.current.scrollTop
        + region.getBoundingClientRect().top - panel.current.getBoundingClientRect().top - 20);
    }
  }
  useLayoutEffect(locate, [blocks.data, targets]);
  return <div className="material-source-view">
    <div className="material-reader-tools">
      <button className="text-button" onClick={onBack}><ArrowLeft size={15} />返回题目</button>
      <button className="text-button" onClick={onOpenOriginal}><ExternalLink size={15} />打开原文件</button>
      <a className="icon-button" href={materialOriginalUrl(documentId)} title="导出原文件" aria-label="导出原文件">
        <Download size={15} /></a>
    </div>
    <div className="material-source-scroll" ref={panel}>
      {blocks.isLoading && <p role="status">正在读取原文…</p>}
      {blocks.error && <p role="alert">{blocks.error.message}</p>}
      {blocks.data?.map(block => {
        const highlights = targets.filter(ref => ref.block_id === block.id);
        return <section key={block.id} data-source={block.id}
          className={`material-source-block ${highlights.length ? "is-target" : ""}`}>
          <header><strong>{sourceLabel(block)}</strong>
            <select aria-label={`${sourceLabel(block)}覆盖状态`} value={block.coverage}
              onChange={event => onAccount(block, event.target.value)}>
              <option value="unprocessed" disabled>未整理</option><option value="identified">已识别内容</option>
              <option value="non_question">非题目内容</option><option value="pending">待处理</option>
            </select>
            <button className="text-button" onClick={() => onCreate(block)}>选为题目</button></header>
          {(block.reason || block.issue) && <p className="material-issue">{block.reason || block.issue}</p>}
          {block.image ? <div className="material-page-image">
            <img src={materialAssetUrl(documentId, block.image)} alt={sourceLabel(block)}
              width={block.width} height={block.height} loading="lazy" />
            {highlights.map((ref, index) => {
              const [left, top, right, bottom] = ref.bbox ?? [0, 0, 1, 1];
              return <div key={index} className="material-highlight" style={{
                left: `${left * 100}%`, top: `${top * 100}%`, width: `${(right - left) * 100}%`,
                height: `${(bottom - top) * 100}%`,
              }} />;
            })}
          </div> : <HighlightedText block={block} refs={highlights} />}
          {block.images?.map(image => <img key={image.image} className="material-question-image"
            src={materialAssetUrl(documentId, image.image)} alt="Word 原文图片" />)}
        </section>;
      })}
    </div>
  </div>;
}

function HighlightedText({ block, refs }: { block: MaterialBlock; refs: MaterialSource[] }) {
  const ref = refs.find(r => r.start !== undefined);
  if (!ref || ref.start === undefined || ref.end === undefined) return <MaterialText text={block.text} />;
  return <pre className="material-source-text">{block.text.slice(0, ref.start)}
    <mark>{block.text.slice(ref.start, ref.end)}</mark>{block.text.slice(ref.end)}</pre>;
}
