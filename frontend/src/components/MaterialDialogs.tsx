import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowDown, ArrowUp, Merge, Plus, Save, Trash2, Upload, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useModalFocus } from "../hooks/useModalFocus";
import { getMaterialBlocks, itemValues, uploadCorrection, type MaterialCategory, type MaterialCollection,
  type MaterialFolder, type MaterialItem, type MaterialSource, type MaterialValues } from "../api/materials";

export function MaterialDialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useModalFocus(true, ref, ref, onClose);
  return <div className="material-modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={ref} tabIndex={-1} className="material-modal" role="dialog" aria-modal="true" aria-label={title}
      onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}>
      <header><h2>{title}</h2><button className="icon-button" title="关闭" aria-label="关闭" onClick={onClose}><X size={17} /></button></header>
      {children}
    </div>
  </div>;
}

export function CollectionEditor({ collection, folders, onSave, onClose }: {
  collection?: MaterialCollection; folders: MaterialFolder[];
  onSave: (value: { name: string; folder_ids: number[] }) => Promise<void>; onClose: () => void;
}) {
  const [name, setName] = useState(collection?.name ?? "");
  const [selected, setSelected] = useState(collection?.folder_ids ?? []);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  function path(folder: MaterialFolder, seen = new Set<number>()): string {
    if (seen.has(folder.id)) return folder.name;
    seen.add(folder.id);
    const parent = folders.find(f => f.id === folder.parent_id);
    return parent ? `${path(parent, seen)} / ${folder.name}` : folder.name;
  }
  return <MaterialDialog title={collection ? "编辑资料集" : "新建资料集"} onClose={onClose}>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError("");
      try { await onSave({ name, folder_ids: selected }); } catch (e) { setError((e as Error).message); }
      finally { setBusy(false); }
    }}>
      <label>资料集名称<input autoFocus required maxLength={120} value={name} onChange={e => setName(e.target.value)} /></label>
      <fieldset><legend>关联视频文件夹（包含子文件夹）</legend>
        {folders.length ? folders.map(folder => <label className="material-checkbox" key={folder.id}>
          <input type="checkbox" checked={selected.includes(folder.id)} onChange={e => setSelected(ids =>
            e.target.checked ? [...ids, folder.id] : ids.filter(id => id !== folder.id))} />{path(folder)}</label>)
          : <p className="material-muted">暂无视频文件夹</p>}
      </fieldset>
      {error && <p role="alert" className="field-error">{error}</p>}
      <footer><button className="primary-button" disabled={busy}><Save size={16} />保存</button></footer>
    </form>
  </MaterialDialog>;
}

export function CategoryEditor({ categories, onSave, onMerge, onClose }: {
  categories: MaterialCategory[];
  onSave: (value: { id?: string; name: string; parent_id: string | null; aliases: string[]; position: number }) => Promise<void>;
  onMerge: (from: string, to: string) => Promise<void>; onClose: () => void;
}) {
  const [selected, setSelected] = useState("");
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [aliases, setAliases] = useState("");
  const [position, setPosition] = useState(categories.length + 1);
  const [target, setTarget] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  function select(id: string) {
    const category = categories.find(c => c.id === id);
    setSelected(id); setName(category?.name ?? ""); setParent(category?.parent_id ?? "");
    setAliases(category?.aliases.join("，") ?? ""); setPosition(category?.position ?? categories.length + 1); setTarget("");
  }
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError("");
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <MaterialDialog title="整理分类目录" onClose={onClose}>
    <label>选择分类<select value={selected} onChange={e => select(e.target.value)}><option value="">新增分类</option>
      {categories.map(c => <option key={c.id} value={c.id}>{c.parent_id ? "　" : ""}{c.name}</option>)}</select></label>
    <form onSubmit={event => { event.preventDefault(); void action(() => onSave({
      id: selected || undefined, name, parent_id: parent || null,
      aliases: aliases.split(/[,，\n]/).map(s => s.trim()).filter(Boolean), position,
    })); }}>
      <label>分类名称<input required maxLength={100} value={name} onChange={e => setName(e.target.value)} /></label>
      <label>所属知识主题<select value={parent} onChange={e => setParent(e.target.value)}>
        <option value="">一级知识主题</option>{categories.filter(c => !c.parent_id && c.id !== selected).map(c =>
          <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
      <label>搜索别名<input value={aliases} onChange={e => setAliases(e.target.value)} /></label>
      <label>目录顺序<div className="material-inline">
        <button type="button" className="icon-button" title="上移" aria-label="上移" onClick={() => setPosition(p => Math.max(0, p - 1))}><ArrowUp size={15} /></button>
        <input aria-label="目录顺序值" type="number" min={0} value={position} onChange={e => setPosition(Number(e.target.value))} />
        <button type="button" className="icon-button" title="下移" aria-label="下移" onClick={() => setPosition(p => p + 1)}><ArrowDown size={15} /></button>
      </div></label>
      <footer><button className="primary-button" disabled={busy}><Save size={16} />保存分类</button></footer>
    </form>
    {selected && <div className="material-merge-control">
      <label>合并到<select value={target} onChange={e => setTarget(e.target.value)}><option value="">选择目标</option>
        {categories.filter(c => c.id !== selected && Boolean(c.parent_id) === Boolean(parent)).map(c =>
          <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
      <button className="secondary-button" disabled={!target || busy} onClick={() => void action(async () => {
        await onMerge(selected, target); select("");
      })}><Merge size={15} />合并分类</button>
    </div>}
    {error && <p role="alert" className="field-error">{error}</p>}
  </MaterialDialog>;
}

export function emptyMaterial(sources: MaterialSource[] = []): MaterialValues {
  return { title: "", number: "", kind: "question", quality: "pending", reason: "等待人工整理",
    sources, category_ids: [], tags: [], parent_id: null, references: [], correction: "",
    correction_image: null, answer_sources: [] };
}

export function ItemEditor({ documentId, item, initial, categories, siblings, onSave, onClose }: {
  documentId: string; item?: MaterialItem; initial?: MaterialValues; categories: MaterialCategory[];
  siblings: MaterialItem[]; onSave: (value: MaterialValues) => Promise<void>; onClose: () => void;
}) {
  const [value, setValue] = useState(initial ?? (item ? itemValues(item) : emptyMaterial()));
  const [tags, setTags] = useState(value.tags.join("，"));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const blocks = useQuery({ queryKey: ["materials", "blocks", documentId], queryFn: () => getMaterialBlocks(documentId) });
  const patch = (partial: Partial<MaterialValues>) => setValue(v => ({ ...v, ...partial }));
  function updateRef(index: number, partial: Partial<MaterialSource>, answer = false) {
    const key = answer ? "answer_sources" : "sources";
    patch({ [key]: value[key].map((r, i) => i === index ? { ...r, ...partial } : r) });
  }
  function sourceInputs(sources: MaterialSource[], answer = false) {
    const key = answer ? "answer_sources" : "sources";
    return <fieldset><legend>{answer ? "可分离的解析区域" : "题目来源区域"}</legend>
      {sources.map((ref, index) => {
        const block = blocks.data?.find(b => b.id === ref.block_id);
        return <div className="material-range-edit" key={index}>
          <div className="material-inline"><select aria-label={`${answer ? "解析" : "题目"}来源 ${index + 1}`}
            value={ref.block_id} onChange={e => {
              patch({ [key]: sources.map((r, i) => i === index ? { block_id: e.target.value } : r) });
            }}><option value="">选择来源块</option>
            {blocks.data?.map(b => <option key={b.id} value={b.id}>
              {b.page ? `第 ${b.page} 页` : b.line_start ? `第 ${b.line_start}–${b.line_end} 行` : `内容块 ${b.ordinal + 1}`}
            </option>)}</select>
            <button type="button" className="icon-button" aria-label="移除来源区域" title="移除来源区域"
              onClick={() => patch({ [key]: sources.filter((_, i) => i !== index) })}><Trash2 size={15} /></button></div>
          {block?.image ? <div className="material-range-coordinates">{["左", "上", "右", "下"].map((label, coordinate) =>
            <label key={label}>{label}（%）<input type="number" min={0} max={100} step={0.1}
              value={Number(((ref.bbox ?? [0, 0, 1, 1])[coordinate] * 100).toFixed(2))}
              onChange={e => updateRef(index, { bbox: (ref.bbox ?? [0, 0, 1, 1]).map((n, i) =>
                i === coordinate ? Number(e.target.value) / 100 : n) }, answer)} /></label>)}</div>
            : <div className="material-inline">
              <label>起始字符<input type="number" min={0} value={ref.start ?? ""}
                onChange={e => updateRef(index, { start: Number(e.target.value), end: ref.end ?? block?.text.length }, answer)} /></label>
              <label>结束字符<input type="number" min={1} max={block?.text.length} value={ref.end ?? ""}
                onChange={e => updateRef(index, { start: ref.start ?? 0, end: Number(e.target.value) }, answer)} /></label>
            </div>}
        </div>;
      })}
      <button type="button" className="text-button" onClick={() => patch({
        [key]: [...sources, { block_id: blocks.data?.[0]?.id ?? "" }],
      })}><Plus size={15} />添加区域</button>
    </fieldset>;
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try { await onSave({ ...value, tags: tags.split(/[,，\n]/).map(s => s.trim()).filter(Boolean) }); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return <MaterialDialog title={item ? "修正题目" : "新增题目"} onClose={onClose}>
    <form onSubmit={event => void save(event)}>
      <label>标题<input required autoFocus maxLength={180} value={value.title} onChange={e => patch({ title: e.target.value })} /></label>
      <div className="material-inline">
        <label>原题号<input value={value.number} onChange={e => patch({ number: e.target.value })} /></label>
        <label>内容类型<select value={value.kind} onChange={e => patch({ kind: e.target.value as MaterialValues["kind"] })}>
          <option value="question">练习题／小问</option><option value="context">大题共享题干</option><option value="answer">解析</option></select></label>
      </div>
      <label>所属共享题干<select value={value.parent_id ?? ""} onChange={e => patch({ parent_id: e.target.value || null })}>
        <option value="">无</option>{siblings.filter(i => i.document_id === documentId && i.kind === "context" && !i.deleted && i.id !== item?.id).map(i =>
          <option key={i.id} value={i.id}>{i.number} {i.title}</option>)}</select></label>
      <fieldset><legend>题型归属</legend>{categories.filter(c => c.parent_id).map(c => <label key={c.id} className="material-checkbox">
        <input type="checkbox" checked={value.category_ids.includes(c.id)} onChange={e => patch({
          category_ids: e.target.checked ? [...value.category_ids, c.id] : value.category_ids.filter(id => id !== c.id),
        })} />{categories.find(p => p.id === c.parent_id)?.name} / {c.name}</label>)}</fieldset>
      <label>辅助标签<input value={tags} onChange={e => setTags(e.target.value)} /></label>
      {sourceInputs(value.sources)}
      {sourceInputs(value.answer_sources, true)}
      <div><label htmlFor="material-correction-text">修正文字</label>
        <textarea id="material-correction-text" rows={5} value={value.correction} onChange={e => patch({ correction: e.target.value })} /></div>
      <label className="material-upload-label"><Upload size={16} />{value.correction_image ? "替换修正题图" : "补充清晰题图"}
        <input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={async e => {
          const file = e.target.files?.[0]; if (!file) return;
          setBusy(true); setError("");
          try { patch({ correction_image: (await uploadCorrection(documentId, file)).resource }); }
          catch (err) { setError((err as Error).message); } finally { setBusy(false); }
        }} /></label>
      {value.correction_image && <button className="text-button" type="button" onClick={() => patch({ correction_image: null })}>取消修正题图</button>}
      <fieldset><legend>引用前题</legend>{value.references.map((ref, index) => <div className="material-reference-edit" key={index}>
        <input aria-label="前题引用原文" value={ref.label} onChange={e => patch({
          references: value.references.map((r, i) => i === index ? { ...r, label: e.target.value } : r),
        })} />
        <select aria-label="引用题目" value={ref.item_id ?? ""} onChange={e => patch({
          references: value.references.map((r, i) => i === index ? { ...r, item_id: e.target.value || null } : r),
        })}><option value="">未找到前题</option>{siblings.filter(i => i.document_id === documentId && i.id !== item?.id && !i.deleted).map(i =>
          <option key={i.id} value={i.id}>{i.number} {i.title}</option>)}</select>
        <button className="icon-button" type="button" title="移除前题引用" aria-label="移除前题引用"
          onClick={() => patch({ references: value.references.filter((_, i) => i !== index) })}><X size={14} /></button>
      </div>)}<button className="text-button" type="button" onClick={() => patch({
        references: [...value.references, { label: "", item_id: null }],
      })}><Plus size={15} />添加引用</button></fieldset>
      <label>审核状态<select value={value.quality} onChange={e => patch({ quality: e.target.value as MaterialValues["quality"] })}>
        <option value="pending">待处理</option><option value="ready">可用</option></select></label>
      {value.quality === "pending" && <label>待处理原因<input required value={value.reason} onChange={e => patch({ reason: e.target.value })} /></label>}
      {error && <p role="alert" className="field-error">{error}</p>}
      <footer><button className="primary-button" disabled={busy || blocks.isLoading}><Save size={16} />保存题目</button></footer>
    </form>
  </MaterialDialog>;
}
