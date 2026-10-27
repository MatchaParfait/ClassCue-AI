import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Check, ChevronLeft, ChevronRight, FileText, Folder, FolderTree,
  Link2, ListFilter, LoaderCircle, MapPin, Merge, Pencil, Play, Plus, RotateCcw, Scissors,
  Search, Settings2, Trash2, Upload } from "lucide-react";
import { cancelJob, getAiSettings, getRecentJobs } from "../api/client";
import { getCategories, getCollections, getDocuments, getMaterialBlocks, getMaterialFolders,
  getMaterialItem, getMaterialItems, getMaterialRuns, itemValues, materialAssetUrl, materialRequest,
  uploadMaterial, type MaterialBlock, type MaterialCollection, type MaterialItem, type MaterialSource,
  type MaterialStudy, type MaterialValues } from "../api/materials";
import { CategoryEditor, CollectionEditor, emptyMaterial, ItemEditor, MaterialDialog } from "../components/MaterialDialogs";
import { MaterialRegions, MaterialSourceView, MaterialText, sourceLabel } from "../components/MaterialReader";
import { isTaskTerminal } from "../hooks/useTaskStatus";
import "./MaterialsPage.css";

interface Navigation {
  collection: string; category: string; document: string; item: string; view: "categories" | "documents" | "pending";
  query: string; status: string; tag: string; recycle: boolean; pane: "directory" | "list" | "reader";
}
const defaults: Navigation = { collection: "", category: "", document: "", item: "", view: "categories",
  query: "", status: "", tag: "", recycle: false, pane: "directory" };
const studyNames: Record<MaterialStudy, string> = { unreviewed: "未复习", consolidate: "待巩固", mastered: "已掌握" };
function loadNavigation(key: string): Navigation {
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? "{}");
    const value = { ...defaults };
    for (const name of Object.keys(defaults) as (keyof Navigation)[]) {
      if (saved && typeof saved[name] === typeof defaults[name]) Object.assign(value, { [name]: saved[name] });
    }
    if (!["categories", "documents", "pending"].includes(value.view)) value.view = "categories";
    if (!["directory", "list", "reader"].includes(value.pane)) value.pane = "directory";
    return value;
  } catch { return defaults; }
}
function saveLocal(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Browser storage is optional. */ } }
function loadScroll(key: string) { try { return Number(localStorage.getItem(key) ?? 0) || 0; } catch { return 0; } }
type Editor = { documentId: string; item?: MaterialItem; initial?: MaterialValues;
  split?: { original: string; first?: MaterialValues }; merge?: string[] };

export function MaterialsPage({ active = true, scope = "default", onJob }: {
  active?: boolean; scope?: string; onJob: (id: string | number) => void;
}) {
  const storageKey = `classcue-materials:${scope}`;
  const [nav, setNav] = useState(() => loadNavigation(storageKey));
  const navigate = (patch: Partial<Navigation>) => setNav(v => ({ ...v, ...patch }));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [collectionEditor, setCollectionEditor] = useState<MaterialCollection | "new" | null>(null);
  const [categoryEditor, setCategoryEditor] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [source, setSource] = useState<{ documentId: string; refs: MaterialSource[] } | null>(null);
  const [answerEditor, setAnswerEditor] = useState(false);
  const [answerIds, setAnswerIds] = useState<string[]>([]);
  const [mergeIds, setMergeIds] = useState<string[]>([]);
  const [version, setVersion] = useState<{ version: number; payload: MaterialValues; study_status: MaterialStudy } | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const reader = useRef<HTMLDivElement>(null);
  const client = useQueryClient();
  const collections = useQuery({ queryKey: ["materials", scope, "collections"], queryFn: getCollections, enabled: active });
  const folders = useQuery({ queryKey: ["materials", scope, "folders"], queryFn: getMaterialFolders, enabled: active });
  const ai = useQuery({ queryKey: ["ai-settings"], queryFn: getAiSettings, enabled: active });
  const jobs = useQuery({ queryKey: ["recent-jobs"], queryFn: getRecentJobs, enabled: active, refetchInterval: active ? 3000 : false });
  const currentJob = jobs.data?.find(j => !isTaskTerminal(j));
  const isOrganizing = currentJob?.job_type === "material_preprocess";
  const documents = useQuery({ queryKey: ["materials", scope, nav.collection, "documents"],
    queryFn: () => getDocuments(nav.collection), enabled: active && !!nav.collection,
    refetchInterval: active && isOrganizing ? 3000 : false });
  const categories = useQuery({ queryKey: ["materials", scope, nav.collection, "categories"],
    queryFn: () => getCategories(nav.collection), enabled: active && !!nav.collection,
    refetchInterval: active && isOrganizing ? 3000 : false });
  const items = useQuery({ queryKey: ["materials", scope, nav.collection, "items"],
    queryFn: () => getMaterialItems(nav.collection), enabled: active && !!nav.collection,
    refetchInterval: active && isOrganizing ? 3000 : false });
  const search = useQuery({ queryKey: ["materials", scope, nav.collection, "search", nav.query],
    queryFn: () => getMaterialItems(nav.collection, nav.query.trim()), enabled: active && !!nav.collection && !!nav.query.trim() });
  const detail = useQuery({ queryKey: ["materials", "item", nav.item], queryFn: () => getMaterialItem(nav.item),
    enabled: active && !!nav.item });
  const runs = useQuery({ queryKey: ["materials", scope, nav.collection, "runs"],
    queryFn: () => getMaterialRuns(nav.collection), enabled: active && !!nav.collection,
    refetchInterval: active && isOrganizing ? 3000 : false });
  const selectedBlocks = useQuery({ queryKey: ["materials", "blocks", nav.document],
    queryFn: () => getMaterialBlocks(nav.document), enabled: active && !!nav.document });
  const collection = collections.data?.find(c => c.id === nav.collection);
  const doc = documents.data?.find(d => d.id === nav.document);
  useEffect(() => {
    if (collections.data?.length && !collections.data.some(c => c.id === nav.collection)) {
      navigate({ ...defaults, collection: collections.data[0].id });
    }
  }, [collections.data, nav.collection]);
  useEffect(() => { if (!connectionId && ai.data?.connection_id) setConnectionId(ai.data.connection_id); }, [ai.data, connectionId]);
  useEffect(() => { saveLocal(storageKey, JSON.stringify(nav)); }, [nav, storageKey]);
  const previousJob = useRef(false);
  useEffect(() => {
    if (previousJob.current && !isOrganizing) void client.invalidateQueries({ queryKey: ["materials"] });
    previousJob.current = isOrganizing;
  }, [isOrganizing, client]);
  useLayoutEffect(() => {
    if (!active || !detail.data || !reader.current) return;
    const key = `${storageKey}:reader:${nav.item}`;
    const panel = reader.current;
    panel.scrollTop = loadScroll(key);
    const save = () => saveLocal(key, String(panel.scrollTop));
    panel.addEventListener("scroll", save, { passive: true });
    return () => { save(); panel.removeEventListener("scroll", save); };
  }, [nav.item, active, detail.data?.id, storageKey]);

  const categoryIds = useMemo(() => new Set([nav.category,
    ...(categories.data ?? []).filter(c => c.parent_id === nav.category).map(c => c.id)]), [nav.category, categories.data]);
  const visibleItems = (items.data ?? []).filter(item => {
    const document = documents.data?.find(d => d.id === item.document_id);
    const removed = !!(item.deleted || document?.deleted);
    if (removed !== nav.recycle || item.kind === "context" && nav.view !== "documents") return false;
    if (nav.document && item.document_id !== nav.document) return false;
    if (nav.view === "pending" && item.quality !== "pending") return false;
    if (nav.view === "categories" && (item.quality !== "ready" || nav.category && !item.category_ids.some(id => categoryIds.has(id)))) return false;
    if (nav.status && item.study_status !== nav.status) return false;
    if (nav.tag && !item.tags.includes(nav.tag)) return false;
    if (nav.query.trim() && !search.data?.some(found => found.id === item.id)) return false;
    return true;
  });
  const selectedIndex = visibleItems.findIndex(i => i.id === nav.item);
  const current = detail.data;
  const allTags = [...new Set((items.data ?? []).flatMap(i => i.tags))];
  const pendingBlocks = selectedBlocks.data?.filter(b => b.coverage === "pending" || b.coverage === "unprocessed") ?? [];
  async function refresh() { await client.invalidateQueries({ queryKey: ["materials"] }); }
  async function action(operation: () => Promise<void>) {
    setBusy(true); setError("");
    try { await operation(); await refresh(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  function selectItem(id: string) { navigate({ item: id, pane: "reader" }); setSource(null); }
  function selectDocument(id: string) {
    navigate({ document: id, item: "", pane: "list" }); setSource(null); setMergeIds([]);
  }
  function newFromBlock(block: MaterialBlock) {
    setEditor({ documentId: block.document_id, initial: emptyMaterial([{ block_id: block.id }]) });
  }
  const latestRun = runs.data?.[0];
  const queryError = collections.error ?? documents.error ?? items.error ?? categories.error ?? detail.error ?? folders.error ?? runs.error ?? search.error;
  const fileInput = useRef<HTMLInputElement>(null);
  const [uploadMessage, setUploadMessage] = useState("");
  const title = nav.view === "categories" ? categories.data?.find(c => c.id === nav.category)?.name ?? "全部题目"
    : nav.view === "documents" ? doc?.name ?? "原始资料" : "待处理";

  return <main className="materials-page">
    <header className="materials-heading">
      <div><h1 tabIndex={-1}>复习资料</h1>{collection && <span className="material-muted">{collection.total} 道题 · 已掌握 {collection.mastered} 道</span>}</div>
      <div className="material-heading-tools">
        <select aria-label="当前资料集" value={nav.collection} onChange={e => {
          navigate({ ...defaults, collection: e.target.value }); setSource(null); setMergeIds([]);
        }}><option value="" disabled>选择资料集</option>{collections.data?.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
        <button className="icon-button" title="编辑资料集" aria-label="编辑资料集" disabled={!collection || busy || !!currentJob}
          onClick={() => setCollectionEditor(collection!)}><Pencil size={16} /></button>
        <button className="secondary-button" disabled={busy || !!currentJob} onClick={() => setCollectionEditor("new")}><Plus size={16} />新建资料集</button>
        <button className="primary-button" disabled={!collection || busy || !!currentJob} onClick={() => fileInput.current?.click()}><Upload size={16} />导入资料</button>
        <input ref={fileInput} hidden type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.webp,.md,.markdown,.docx"
          onChange={event => {
            const files = [...(event.target.files ?? [])]; event.target.value = "";
            if (!files.length) return;
            void action(async () => {
              const failed: string[] = [];
              for (let index = 0; index < files.length; index++) {
                setUploadMessage(`正在导入 ${index + 1}/${files.length}：${files[index].name}`);
                try { await uploadMaterial(nav.collection, files[index]); }
                catch (e) { failed.push(`${files[index].name}：${(e as Error).message}`); }
              }
              setUploadMessage(""); navigate({ view: "documents", document: "", pane: "directory" });
              if (failed.length) { await refresh(); throw new Error(failed.join("；")); }
            });
          }} />
      </div>
    </header>
    {(error || queryError || ai.error) && <div className="material-error" role="alert">
      <span>{error || queryError?.message || ai.error?.message}</span><button className="text-button" onClick={() => void refresh()}>重新读取</button>
    </div>}
    {uploadMessage && <p role="status">{uploadMessage}</p>}
    {collections.isLoading && <p role="status">正在读取资料集…</p>}
    {!collections.isLoading && !collections.data?.length && !collections.error
      ? <div className="material-empty"><BookOpen size={32} /><h2>暂无复习资料</h2>
        <button className="primary-button" onClick={() => setCollectionEditor("new")}><Plus size={16} />新建资料集</button></div>
      : collection && <>
        <div className="materials-processing">
          <label>整理连接<select aria-label="整理连接" disabled={busy || !!currentJob} value={connectionId} onChange={e => setConnectionId(e.target.value)}>
            <option value="">选择 AI 连接</option>{ai.data?.connections?.map(c => <option key={c.id} value={c.id}>{c.name} · {c.model}</option>)}</select></label>
          <button className="secondary-button" disabled={busy || !!currentJob || !connectionId || !documents.data?.some(d => !d.deleted && (d.remaining || d.state === "failed" || d.state === "imported"))}
            onClick={() => void action(async () => {
              const result = await materialRequest<{ job_id: string }>(`/api/material-collections/${nav.collection}/process`, "POST", { connection_id: connectionId });
              onJob(result.job_id); await client.invalidateQueries({ queryKey: ["recent-jobs"] });
            })}><Play size={15} />{latestRun && latestRun.state !== "completed" ? "继续未完成整理" : "自动整理"}</button>
          {isOrganizing ? <><span role="status" className="material-task"><LoaderCircle size={15} className="spin" />{currentJob.message}</span>
            <button className="text-button" disabled={busy} onClick={() => void action(async () => { await cancelJob(currentJob.id); })}>停止</button></>
            : latestRun && latestRun.state !== "completed" && <span className="material-muted">{latestRun.reason}</span>}
        </div>
        <div className="materials-workspace" data-pane={nav.pane}>
          <aside className="material-directory" aria-label="资料目录">
            <div className="material-view-tabs" role="tablist" aria-label="资料组织视图">
              {([{ id: "categories", label: "按知识与题型", Icon: FolderTree }, { id: "documents", label: "按原始资料", Icon: FileText },
                { id: "pending", label: "待处理", Icon: ListFilter }] as const).map(({ id, label, Icon }) =>
                <button key={id} role="tab" aria-selected={nav.view === id} onClick={() => {
                  navigate({ view: id, category: "", document: "", item: "", pane: id === "pending" ? "list" : "directory" });
                  setSource(null);
                }}><Icon size={15} />{label}</button>)}
            </div>
            <div className="material-directory-scroll">
              {nav.view === "categories" ? <>
                <div className="material-directory-title"><strong>知识与题型</strong>
                  <button className="icon-button" title="整理分类目录" aria-label="整理分类目录" disabled={busy || !!currentJob} onClick={() => setCategoryEditor(true)}><Settings2 size={15} /></button></div>
                <button className="material-directory-row" aria-pressed={!nav.category} onClick={() => navigate({ category: "", document: "", pane: "list" })}>全部题目<span>{collection.total}</span></button>
                {categories.data?.filter(c => !c.parent_id).map(topic => <div key={topic.id}>
                  <button className="material-directory-row material-topic" aria-pressed={nav.category === topic.id}
                    onClick={() => navigate({ category: topic.id, document: "", pane: "list" })}><Folder size={15} />{topic.name}<span>{topic.count}</span></button>
                  {categories.data.filter(c => c.parent_id === topic.id).map(type => <button key={type.id}
                    className="material-directory-row material-type" aria-pressed={nav.category === type.id}
                    onClick={() => navigate({ category: type.id, document: "", pane: "list" })}>{type.name}<span>{type.count}</span></button>)}
                </div>)}
                {!categories.data?.length && <p className="material-muted material-empty-note">暂无分类</p>}
              </> : <>
                <div className="material-directory-title"><strong>原始资料</strong></div>
                <button className="material-directory-row" aria-pressed={!nav.document} onClick={() => selectDocument("")}>全部资料</button>
                {documents.data?.filter(d => Boolean(d.deleted) === nav.recycle).map(document => <div className="material-document-row" key={document.id}>
                  <button className="material-directory-row" aria-pressed={nav.document === document.id} onClick={() => selectDocument(document.id)}>
                    <FileText size={15} /><span className="material-document-name">{document.name}<small>{document.reason || `${document.block_count} 个来源块 · 未整理 ${document.remaining}`}</small></span>
                  </button>
                  <button className="icon-button" title={document.deleted ? "恢复文件" : "移除文件"} aria-label={`${document.deleted ? "恢复文件" : "移除文件"} ${document.name}`}
                    disabled={busy || !!currentJob} onClick={() => void action(async () => {
                      await materialRequest(`/api/material-documents/${document.id}${document.deleted ? "/restore" : ""}`, document.deleted ? "POST" : "DELETE");
                    })}>{document.deleted ? <RotateCcw size={14} /> : <Trash2 size={14} />}</button>
                </div>)}
              </>}
            </div>
            <label className="material-checkbox material-recycle"><input type="checkbox" checked={nav.recycle}
              onChange={e => navigate({ recycle: e.target.checked, item: "" })} /><Trash2 size={14} />已移除内容</label>
          </aside>
          <section className="material-list" aria-label="题目列表">
            <div className="material-list-heading">
              <button className="icon-button material-mobile-back" title="返回目录" aria-label="返回目录"
                onClick={() => navigate({ pane: "directory" })}><ArrowLeft size={16} /></button>
              <h2>{title}</h2><span className="material-muted">{visibleItems.length} 条</span>
            </div>
            <div className="material-filters">
              <label className="material-search"><Search size={15} /><input aria-label="搜索题目" placeholder="搜索题名、标签、分类别名" value={nav.query} onChange={e => navigate({ query: e.target.value })} /></label>
              <div className="material-inline">
                <select aria-label="来源筛选" value={nav.document} onChange={e => navigate({ document: e.target.value })}>
                  <option value="">全部来源</option>{documents.data?.filter(d => Boolean(d.deleted) === nav.recycle).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
                <select aria-label="学习状态筛选" value={nav.status} onChange={e => navigate({ status: e.target.value })}><option value="">全部状态</option>
                  {Object.entries(studyNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
              </div>
              <select aria-label="辅助标签筛选" value={nav.tag} onChange={e => navigate({ tag: e.target.value })}><option value="">全部辅助标签</option>
                {allTags.map(tag => <option key={tag} value={tag}>{tag}</option>)}</select>
            </div>
            <div className="material-list-scroll">
              {nav.document && nav.view !== "categories" && <>
                <button className="material-source-entry" onClick={() => { setSource({ documentId: nav.document, refs: [] }); navigate({ pane: "reader" }); }}><MapPin size={15} />查看整份原文<ChevronRight size={15} /></button>
                {nav.view === "pending" && pendingBlocks.map(block => <button key={block.id} className="material-pending-block"
                  onClick={() => { setSource({ documentId: nav.document, refs: [{ block_id: block.id }] }); navigate({ pane: "reader" }); }}>
                  <strong>{sourceLabel(block)}</strong><span>{block.reason || "尚未整理"}</span></button>)}
                {doc?.state === "failed" && <div className="material-issue">{doc.reason}</div>}
              </>}
              {items.isLoading && <p role="status">正在读取题目…</p>}
              {visibleItems.map(item => <div className={`material-item-row ${nav.item === item.id ? "selected" : ""}`} key={item.id}>
                <input type="checkbox" aria-label={`合并选择 ${item.title}`} checked={mergeIds.includes(item.id)}
                  onChange={e => setMergeIds(ids => e.target.checked ? [...ids, item.id] : ids.filter(id => id !== item.id))} />
                <button onClick={() => selectItem(item.id)} aria-pressed={nav.item === item.id}>
                  <strong>{item.kind === "answer" && <span className="material-answer-badge">解析</span>}{item.title}</strong>
                  <span className="material-source-name">{item.document_name} {item.number && `· ${item.number}`}</span>
                  {item.quality === "pending" && <span className="material-issue">{item.reason}</span>}
                  <span className="material-item-meta"><span>{item.tags.join(" · ")}</span>
                    {item.kind === "question" && <small data-study={item.study_status}>{studyNames[item.study_status]}</small>}</span>
                </button>
              </div>)}
              {!visibleItems.length && !items.isLoading && <p className="material-empty-note">当前筛选下暂无条目</p>}
              {nav.view === "pending" && !nav.document && <div className="material-pending-documents">
                {documents.data?.filter(d => !d.deleted && (d.remaining || d.pending_count || d.state === "failed" || d.state === "imported")).map(d =>
                  <button className="material-pending-block" key={d.id} onClick={() => selectDocument(d.id)}>
                    <strong>{d.name}</strong><span>{d.reason || `未整理 ${d.remaining} · 待处理 ${d.pending_count}`}</span></button>)}
              </div>}
            </div>
            <footer className="material-list-footer">
              <button className="text-button" disabled={!nav.document || busy || !!currentJob} onClick={() => setEditor({ documentId: nav.document })}><Plus size={15} />手动新增</button>
              <button className="text-button" disabled={mergeIds.length < 2 || busy || !!currentJob} onClick={() => {
                const rows = (items.data ?? []).filter(i => mergeIds.includes(i.id));
                if (new Set(rows.map(i => i.document_id)).size !== 1) { setError("请选择同一文件内的片段"); return; }
                setEditor({ documentId: rows[0].document_id, merge: mergeIds, initial: {
                  ...itemValues(rows[0]), sources: rows.flatMap(r => r.sources),
                  category_ids: [...new Set(rows.flatMap(r => r.category_ids))],
                } });
              }}><Merge size={15} />合并片段</button>
            </footer>
          </section>
          <section className="material-reader" aria-label="题目阅读">
            {source ? <MaterialSourceView key={source.documentId} documentId={source.documentId} targets={source.refs}
              onCreate={newFromBlock} onBack={() => { setSource(null); if (!nav.item) navigate({ pane: "list" }); }}
              onOpenOriginal={() => void action(async () => {
                await materialRequest(`/api/material-documents/${source.documentId}/open`, "POST");
              })}
              onAccount={(block, state) => void action(async () => {
                await materialRequest(`/api/material-documents/${source.documentId}/blocks/${block.id}/coverage`, "PUT",
                  { state, reason: state === "pending" ? block.reason || "等待人工核对" : "" });
              })} />
              : current ? <>
                <div className="material-reader-tools">
                  <button className="icon-button material-mobile-back" title="返回题目列表" aria-label="返回题目列表" onClick={() => navigate({ pane: "list" })}><ArrowLeft size={16} /></button>
                  <button className="icon-button" title="上一题" aria-label="上一题" disabled={selectedIndex <= 0} onClick={() => selectItem(visibleItems[selectedIndex - 1].id)}><ChevronLeft size={17} /></button>
                  <span>{selectedIndex >= 0 ? `${selectedIndex + 1} / ${visibleItems.length}` : "关联条目"}</span>
                  <button className="icon-button" title="下一题" aria-label="下一题" disabled={selectedIndex < 0 || selectedIndex >= visibleItems.length - 1}
                    onClick={() => selectItem(visibleItems[selectedIndex + 1].id)}><ChevronRight size={17} /></button>
                  <button className="text-button" onClick={() => setSource({ documentId: current.document_id, refs: current.sources })}><MapPin size={15} />原文定位</button>
                </div>
                <div className="material-reading-scroll" ref={reader}>
                  <div className="material-reading-heading"><span className="material-muted">{current.document_name} · {current.number}</span><h2>{current.title}</h2>
                    <span className="material-origin">{current.origin === "automatic" ? "自动整理" : "人工整理"} · 版本 {current.version}</span></div>
                  {current.quality === "pending" && <p className="material-issue">{current.reason}</p>}
                  {current.shared && <section className="material-shared"><header><h3>共享题干</h3>
                    <button className="text-button" onClick={() => selectItem(current.shared!.id)}>整道大题<ChevronRight size={14} /></button></header>
                    <MaterialRegions documentId={current.document_id} regions={current.shared.regions} />
                  </section>}
                  {current.correction_image && <img className="material-question-image" src={materialAssetUrl(current.document_id, current.correction_image)} alt="人工修正题图" />}
                  {current.correction && <MaterialText text={current.correction} />}
                  {current.correction || current.correction_image
                    ? <details><summary>查看原始题目</summary><MaterialRegions documentId={current.document_id} regions={current.regions} /></details>
                    : <MaterialRegions documentId={current.document_id} regions={current.regions} onLocate={refs => setSource({ documentId: current.document_id, refs })} />}
                  {current.references.map((ref, index) => <div className="material-reference" key={index}>
                    <Link2 size={14} />{ref.label}{ref.item_id
                      ? <button className="text-button" onClick={() => selectItem(ref.item_id!)}>查看前题</button> : <span className="material-issue">引用待补充</span>}</div>)}
                  {current.kind === "context" && <div className="material-child-items">{items.data?.filter(i => i.parent_id === current.id && !i.deleted).map(i =>
                    <button className="material-source-entry" key={i.id} onClick={() => selectItem(i.id)}>{i.number} {i.title}<ChevronRight size={14} /></button>)}</div>}
                  {!!current.answer_regions.length && <details className="material-answer"><summary>查看原有解析</summary>
                    <MaterialRegions documentId={current.document_id} regions={current.answer_regions} /></details>}
                  {!!current.answers.length && <section className="material-answer"><h3>关联解析</h3>{current.answers.map(a =>
                    <button className="material-source-entry" key={a.id} onClick={() => selectItem(a.id)}>{a.title}<ChevronRight size={14} /></button>)}</section>}
                  <div className="material-item-categories">{current.category_ids.map(id => <span key={id}>{categories.data?.find(c => c.id === id)?.name}</span>)}</div>
                  {current.versions.length > 0 && <label className="material-history">历史版本<select aria-label="查看历史版本" defaultValue=""
                    onChange={e => { if (e.target.value) void action(async () => {
                      const saved = await materialRequest<{ version: number; payload: MaterialValues; study_status: MaterialStudy }>(
                        `/api/material-items/${current.id}/versions/${e.target.value}`, "GET"); setVersion(saved);
                    }); e.target.value = ""; }}>
                    <option value="">选择版本</option>{current.versions.map(v => <option key={v.version} value={v.version}>版本 {v.version} · {studyNames[v.study_status]}</option>)}</select></label>}
                </div>
                <footer className="material-reading-footer">
                  {current.kind === "question" && !current.deleted && !current.document_deleted && <div className="material-study-control" role="group" aria-label="学习状态">
                    {Object.entries(studyNames).map(([status, label]) => <button key={status} aria-pressed={current.study_status === status}
                      disabled={busy} onClick={() => void action(async () => {
                        await materialRequest(`/api/material-items/${current.id}/study`, "PUT", { status, version: current.version });
                      })}>{current.study_status === status && <Check size={14} />}{label}</button>)}</div>}
                  <div className="material-item-tools">
                    <button className="icon-button" title="修正题目" aria-label="修正题目" disabled={busy || !!currentJob || !!current.deleted || !!current.document_deleted}
                      onClick={() => setEditor({ documentId: current.document_id, item: current })}><Pencil size={16} /></button>
                    <button className="icon-button" title="拆分为两段" aria-label="拆分为两段" disabled={busy || !!currentJob || !!current.deleted || !!current.document_deleted}
                      onClick={() => setEditor({ documentId: current.document_id, split: { original: current.id },
                        initial: { ...itemValues(current), title: `${current.title}（1）` } })}><Scissors size={16} /></button>
                    {current.kind === "question" && <button className="icon-button" title="关联解析" aria-label="关联解析"
                      disabled={busy || !!currentJob || !!current.deleted || !!current.document_deleted}
                      onClick={() => { setAnswerIds(current.answers.map(a => a.id)); setAnswerEditor(true); }}><Link2 size={16} /></button>}
                    <button className="icon-button" title={current.deleted ? "恢复题目" : "移除题目"} aria-label={current.deleted ? "恢复题目" : "移除题目"}
                      disabled={busy || !!currentJob || !!current.document_deleted} onClick={() => void action(async () => {
                        await materialRequest(`/api/material-items/${current.id}${current.deleted ? "/restore" : ""}`, current.deleted ? "POST" : "DELETE");
                      })}>{current.deleted ? <RotateCcw size={16} /> : <Trash2 size={16} />}</button>
                  </div>
                </footer>
              </> : <div className="material-empty">
                <button className="text-button material-mobile-back" onClick={() => navigate({ pane: "list" })}><ArrowLeft size={15} />返回列表</button>
                <BookOpen size={28} /><span>{detail.isLoading ? "正在读取题目…" : "选择一道题目"}</span></div>}
          </section>
        </div>
      </>}
    {collectionEditor && <CollectionEditor collection={collectionEditor === "new" ? undefined : collectionEditor}
      folders={folders.data ?? []} onClose={() => setCollectionEditor(null)} onSave={async value => {
        const saved = await materialRequest<MaterialCollection>(`/api/material-collections${collectionEditor === "new" ? "" : `/${collectionEditor.id}`}`,
          collectionEditor === "new" ? "POST" : "PUT", value);
        await refresh(); navigate({ ...defaults, collection: saved.id }); setCollectionEditor(null);
      }} />}
    {categoryEditor && <CategoryEditor categories={categories.data ?? []} onClose={() => setCategoryEditor(false)}
      onSave={async ({ id, ...value }) => {
        await materialRequest(`/api/material-categories${id ? `/${id}` : ""}`, id ? "PUT" : "POST", { ...value, collection_id: nav.collection }); await refresh();
      }} onMerge={async (from, to) => {
        await materialRequest(`/api/material-categories/${from}/merge`, "POST", { ids: [to] }); await refresh();
        if (nav.category === from) navigate({ category: to });
      }} />}
    {editor && <ItemEditor key={`${editor.item?.id ?? "new"}:${editor.split?.first ? "second" : "first"}`} {...editor}
      categories={categories.data ?? []} siblings={items.data ?? []} onClose={() => setEditor(null)} onSave={async values => {
        if (editor.split && !editor.split.first) {
          setEditor({ ...editor, split: { ...editor.split, first: values },
            initial: { ...values, title: `${current?.title ?? values.title}（2）` } }); return;
        }
        let id: string;
        if (editor.split) {
          const saved = await materialRequest<{ ids: string[] }>(`/api/material-items/${editor.split.original}/split`, "POST",
            { parts: [editor.split.first, values] }); id = saved.ids[0];
        } else if (editor.merge) {
          id = (await materialRequest<{ id: string }>("/api/material-items/merge", "POST", { ids: editor.merge, values })).id;
        } else {
          id = (await materialRequest<MaterialItem>(`/api/material-items${editor.item ? `/${editor.item.id}` : ""}`,
            editor.item ? "PUT" : "POST", { document_id: editor.documentId, values })).id;
        }
        await refresh(); setEditor(null); setMergeIds([]); selectItem(id);
      }} />}
    {answerEditor && current && <MaterialDialog title="手动关联解析" onClose={() => setAnswerEditor(false)}>
      <div className="material-answer-choices">{items.data?.filter(i => i.kind === "answer" && !i.deleted && !documents.data?.find(d => d.id === i.document_id)?.deleted).map(answer =>
        <label key={answer.id} className="material-checkbox"><input type="checkbox" checked={answerIds.includes(answer.id)}
          onChange={e => setAnswerIds(ids => e.target.checked ? [...ids, answer.id] : ids.filter(id => id !== answer.id))} />
          {answer.title} · {answer.document_name}</label>)}</div>
      {!items.data?.some(i => i.kind === "answer" && !i.deleted) && <p className="material-muted">暂无独立解析条目</p>}
      <footer><button className="primary-button" disabled={busy} onClick={() => void action(async () => {
        await materialRequest(`/api/material-items/${current.id}/answers`, "PUT", { ids: answerIds }); setAnswerEditor(false);
      })}><Link2 size={15} />保存关联</button></footer>
    </MaterialDialog>}
    {version && current && <MaterialDialog title={`历史版本 ${version.version}`} onClose={() => setVersion(null)}>
      <h3>{version.payload.title}</h3><p className="material-muted">{studyNames[version.study_status]}</p>
      {version.payload.correction && <MaterialText text={version.payload.correction} />}
      {version.payload.correction_image && <img className="material-question-image" src={materialAssetUrl(current.document_id, version.payload.correction_image)} alt="历史版本题图" />}
      <button className="text-button" onClick={() => { setSource({ documentId: current.document_id, refs: version.payload.sources }); setVersion(null); }}>
        <MapPin size={15} />查看此版本来源</button>
    </MaterialDialog>}
  </main>;
}
