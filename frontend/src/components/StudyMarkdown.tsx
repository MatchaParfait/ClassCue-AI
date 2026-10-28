import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

export function StudyMarkdown({ text }: { text: string }) {
  return <div className="study-markdown"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore" }]]}
    components={{
      img: ({ alt }) => <span className="study-missing-image">[缺少图片：{alt || "未命名图片"}]</span>,
      a: ({ children }) => <span>{children}</span>,
    }}>{text}</ReactMarkdown></div>;
}
