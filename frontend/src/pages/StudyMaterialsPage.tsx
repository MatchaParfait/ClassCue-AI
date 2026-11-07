import { useEffect, useLayoutEffect, useRef, useState, type ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowLeft, ArrowUp, BookOpen, Check, ChevronLeft, ChevronRight, Crop, FileText,
  ImagePlus, List, LoaderCircle, MapPin, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen,
  Pencil, Plus, RotateCcw, Save, Trash2, Undo2, Upload, X, ZoomIn, ZoomOut, ScanLine, Maximize2, Minimize2 } from "lucide-react";
import { getStudyBlocks, getStudyDocuments, getStudyQuestions, getStudyTextBounds, saveStudyQuestion, saveStudyReading,
  studyRegionUrl, studySourceLabel, studyWrite, uploadStudyAttachment, uploadStudyDocument,
  type StudyBlock, type StudyDocument, type StudyQuestion, type StudyRegion } from "../api/studyMaterials";
import { StudyPageCanvas } from "../components/StudyPageCanvas";
import { StudyMarkdown } from "../components/StudyMarkdown";
import { StudyQuestionContent, StudyQuestionPreview } from "../components/StudyQuestionPreview";
import { DshQuestionActions } from "../components/DshQuestionActions";
import { useUnsavedDraft, useUnsavedLeave } from "../components/UnsavedChanges";
import { useStudyMarkdownBoxSelection } from "../hooks/useStudyMarkdownBoxSelection";
import { expandToTextWidth, useStudyPageBoxSelection } from "../hooks/useStudyPageBoxSelection";
import "./StudyMaterialsPage.css";

type Draft = { id?: string; title: string; number: string; regions: StudyRegion[] };
type DraftEntry = { key: string; order: number; value: Draft; baseline: Draft; history: Draft[] };
type View = "files" | "reader" | "questions";
const emptyDraft = (): Draft => ({ title: "", number: "", regions: [] });
const snapshot = (value: Draft) => JSON.parse(JSON.stringify(value)) as Draft;
const same = (a: Draft, b: Draft) => JSON.stringify(a) === JSON.stringify(b);
function savedDocument(key: string) { try { return localStorage.getItem(key) || ""; } catch { return ""; } }
function remember(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* Optional browser storage. */ } }

export function StudyMaterialsPage({ active = true, scope = "default", courseId, requestedDocumentId, onDocumentChange }: {
  active?: boolean; scope?: string; courseId?: string;
  requestedDocumentId?: string; onDocumentChange?: (id: string) => void;
}) {
  const client = useQueryClient();
  const selectionKey = `classcue-study-document:${scope}${courseId ? `:${courseId}` : ""}`;
  const [documentId, setDocumentId] = useState(() => requestedDocumentId || savedDocument(selectionKey));
  // A list refetch may return before a pending reading-position save reaches the server.
  const initialReading = useRef({ id: documentId, value: client.getQueryData<StudyDocument[]>(
    ["study", scope, "documents", courseId])?.find(item => item.id === documentId)?.reading });
  const [view, setView] = useState<View>("files");
  const [leftOpen, setLeftOpen] = useState(true), [rightOpen, setRightOpen] = useState(true);
  const [recycle, setRecycle] = useState(false), [query, setQuery] = useState("");
  const [selecting, setSelecting] = useState(false), [selectedIndex, setSelectedIndex] = useState(-1);
  const [appendRegionMode, setAppendRegionMode] = useState(false);
  const [editableIndex, setEditableIndex] = useState(-1);
  const [snapText, setSnapText] = useState(true);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft), [baseline, setBaseline] = useState<Draft>(emptyDraft);
  const draftRef = useRef(draft), committed = useRef(draft);
  const [history, setHistory] = useState<Draft[]>([]);
  const [parked, setParked] = useState<DraftEntry[]>([]);
  const parkedRef = useRef<DraftEntry[]>([]);
  const currentKey = useRef("initial");
  const currentOrder = useRef(0), nextOrder = useRef(0);
  const [busy, setBusy] = useState(false), [uploading, setUploading] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [uploadProgress, setUploadProgress] = useState<{ name: string; index: number; count: number } | null>(null);
  const [importErrors, setImportErrors] = useState<string[]>([]);
  const [renamingDocument, setRenamingDocument] = useState("");
  const [documentName, setDocumentName] = useState("");
  const [zoom, setZoom] = useState(1), [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const reader = useRef<HTMLDivElement>(null), files = useRef<HTMLInputElement>(null), attachment = useRef<HTMLInputElement>(null);
  const markdownSurface = useRef<HTMLDivElement>(null);
  const markdownAnchor = useRef(-1);
  const focusPanes = useRef({ left: true, right: true });
  const restoring = useRef("");
  const readingStates = useRef(new Map<string, { zoom: number; page: number; question_id: string | null;
    left_open: boolean; right_open: boolean; scroll_top: number }>());
  const readingWrites = useRef(new Map<string, Promise<unknown>>());
  const selectedDocument = useRef(documentId);
  selectedDocument.current = documentId;
  const leave = useUnsavedLeave();
  const documentsKey = ["study", scope, "documents", courseId];
  const documents = useQuery({ queryKey: documentsKey, queryFn: () => getStudyDocuments(courseId), enabled: active });
  const document = documents.data?.find(item => item.id === documentId);
  const available = (documents.data || []).filter(item => item.deleted === recycle && item.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const readable = Boolean(document && !document.deleted);
  const blocks = useQuery({ queryKey: ["study", scope, documentId, "blocks"], queryFn: () => getStudyBlocks(documentId), enabled: active && readable });
  const questions = useQuery({ queryKey: ["study", scope, documentId, "questions"], queryFn: () => getStudyQuestions(documentId), enabled: active && readable });
  const isDirty = !same(draft, baseline);
  const dirtyCount = parked.filter(entry => !same(entry.value, entry.baseline)).length + Number(isDirty);
  const pageBox = useStudyPageBoxSelection({
    enabled: active && selecting && !busy,
    sourceKey: documentId, surface: markdownSurface, snap: snapText,
    onError: setError,
    onSelect: async (regions, snap, replaceIndex) => {
      const sourceDocument = documentId;
      setBusy(true); setError("");
      try {
        const original = replaceIndex === undefined ? undefined : draftRef.current.regions[replaceIndex];
        const group = original?.selection_group || crypto.randomUUID();
        const values: StudyRegion[] = [];
        for (const region of regions) {
          const block = blocks.data?.find(block => block.id === region.block_id);
          let bbox = region.bbox!;
          if (snap && block?.kind === "pdf_page") {
            // Ensure even a quickly crossed page has its maximum width before publishing.
            const bounds = await client.fetchQuery({ queryKey: ["study", scope, documentId, block.id, "text-bounds"],
              queryFn: () => getStudyTextBounds(documentId, block.id), staleTime: Infinity, retry: false }).catch(() => undefined);
            bbox = expandToTextWidth(bbox, bounds, block.width || 600);
          }
          values.push({ ...region, bbox, selection_group: group });
        }
        if (selectedDocument.current !== sourceDocument) return;
        if (replaceIndex === undefined && !appendRegionMode && draftRef.current.regions.length) startFreshDraft();
        const existing = draftRef.current.regions;
        let next = [...existing, ...values], index = next.length - 1;
        if (replaceIndex !== undefined && original) {
          const members = existing.flatMap((region, position) => position === replaceIndex
            || (original.selection_group && region.selection_group === original.selection_group) ? [position] : []);
          const merged = new Map<string, StudyRegion>();
          for (const region of [...members.filter(position => position !== replaceIndex).map(position => existing[position]), ...values]) {
            const previous = merged.get(region.block_id), box = region.bbox!;
            merged.set(region.block_id, { ...region, selection_group: group, bbox: previous?.bbox ? [
              Math.min(previous.bbox[0], box[0]), Math.min(previous.bbox[1], box[1]),
              Math.max(previous.bbox[2], box[2]), Math.max(previous.bbox[3], box[3]),
            ] : box });
          }
          const replacement = [...merged.values()].sort((a, b) => (blocks.data?.find(block => block.id === a.block_id)?.ordinal || 0)
            - (blocks.data?.find(block => block.id === b.block_id)?.ordinal || 0));
          next = existing.filter((_, position) => !members.includes(position));
          next.splice(members[0], 0, ...replacement);
          index = members[0] + replacement.length - 1;
        }
        if (next.length > 100) { setError("一道题最多保存 100 个区域，请缩小选择范围"); return; }
        updateDraft({ ...draftRef.current, regions: next });
        setSelectedIndex(index); setEditableIndex(index); setRightOpen(true);
        setAppendRegionMode(false);
      } catch (failure) { setError((failure as Error).message); }
      finally { setBusy(false); }
    },
  });
  const markdownBox = useStudyMarkdownBoxSelection({
    enabled: active && selecting && !busy && document?.format === "markdown", sourceKey: documentId, surface: markdownSurface,
    onSelect: ids => {
      const selected = (blocks.data || []).filter(block => block.kind === "markdown" && ids.includes(block.id));
      if (!appendRegionMode && draftRef.current.regions.length) startFreshDraft();
      const existing = draftRef.current;
      const selectedIds = new Set(selected.map(block => block.id));
      const regions = [...existing.regions.filter(region => !selectedIds.has(region.block_id)),
        ...selected.map(block => ({ block_id: block.id, start: 0, end: block.text?.length || 0 }))];
      regions.sort((a, b) => (blocks.data?.find(block => block.id === a.block_id)?.ordinal || 0)
        - (blocks.data?.find(block => block.id === b.block_id)?.ordinal || 0));
      updateDraft({ ...existing, regions }); setRightOpen(true);
      markdownAnchor.current = (blocks.data || []).filter(block => block.kind === "markdown").findIndex(block => block.id === selected.at(-1)?.id);
      setAppendRegionMode(false);
    },
  });

  function updateDraft(next: Draft, record = true) {
    draftRef.current = next; setDraft(next);
    if (record) {
      const previousSnapshot = snapshot(committed.current);
      if (!same(previousSnapshot, next)) setHistory(previous => [...previous, previousSnapshot].slice(-50));
      committed.current = snapshot(next);
    }
  }
  function commitGesture() {
    const next = draftRef.current;
    const previousSnapshot = snapshot(committed.current);
    if (!same(previousSnapshot, next)) setHistory(previous => [...previous, previousSnapshot].slice(-50));
    committed.current = snapshot(next);
  }
  function replaceDraft(next: Draft) {
    draftRef.current = next; committed.current = snapshot(next);
    setDraft(next); setBaseline(snapshot(next)); setHistory([]); setSelectedIndex(-1); setEditableIndex(-1); setPreviewOpen(false);
    setAppendRegionMode(false);
    markdownAnchor.current = -1;
  }
  function storeEntries(entries: DraftEntry[]) {
    const ordered = [...entries].sort((a, b) => a.order - b.order);
    parkedRef.current = ordered; setParked(ordered);
  }
  function parkCurrent() {
    const others = parkedRef.current.filter(entry => entry.key !== currentKey.current);
    if (draftRef.current.regions.length || !same(draftRef.current, baseline)) {
      others.push({ key: currentKey.current, order: currentOrder.current, value: snapshot(draftRef.current), baseline: snapshot(baseline), history });
    }
    storeEntries(others);
  }
  function startFreshDraft() {
    parkCurrent(); currentKey.current = crypto.randomUUID(); currentOrder.current = ++nextOrder.current; replaceDraft(emptyDraft());
  }
  function activateEntry(key: string) {
    if (key === currentKey.current || busy) return;
    const entry = parkedRef.current.find(item => item.key === key);
    if (!entry) return;
    parkCurrent();
    storeEntries(parkedRef.current.filter(item => item.key !== key));
    currentKey.current = key; currentOrder.current = entry.order; replaceDraft(snapshot(entry.value));
    setBaseline(snapshot(entry.baseline)); setHistory(entry.history);
    setRightOpen(true); setNotice("");
  }
  function resetDrafts() {
    storeEntries([]); currentKey.current = crypto.randomUUID(); currentOrder.current = 0; nextOrder.current = 0; replaceDraft(emptyDraft());
  }
  function discardAll() {
    storeEntries([]); replaceDraft(snapshot(baseline)); setSelecting(false);
  }
  function undo() {
    if (!history.length) return;
    const previous = history.at(-1)!;
    draftRef.current = snapshot(previous); committed.current = snapshot(previous); setDraft(snapshot(previous));
    const index = previous.regions.length - 1;
    setHistory(values => values.slice(0, -1)); setSelectedIndex(index);
    setEditableIndex(previous.regions[index]?.bbox ? index : -1);
  }
  async function refresh() {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["study", scope] }),
      client.invalidateQueries({ queryKey: ["study-courses", scope] }),
    ]);
  }
  async function save(all = false) {
    const entries: DraftEntry[] = [...(all ? parkedRef.current : []),
      { key: currentKey.current, order: currentOrder.current, value: snapshot(draftRef.current), baseline, history }];
    const pending = entries.filter(entry => !same(entry.value, entry.baseline)).sort((a, b) => a.order - b.order);
    if (!documentId || !pending.length || pending.some(entry => !entry.value.regions.length)) {
      setError("请先选择题目的原文范围"); throw new Error("请先选择题目的原文范围");
    }
    setBusy(true); setError("");
    try {
      for (const entry of pending) {
        const values = entry.value;
        const result = await saveStudyQuestion({ document_id: documentId, title: values.title, number: values.number, regions: values.regions }, values.id);
        const saved: Draft = { id: result.id, title: result.title, number: result.number,
          regions: result.regions.map(({ block_id, bbox, start, end, selection_group }) => ({ block_id, ...(selection_group ? { selection_group } : {}), ...(bbox ? { bbox } : { start, end }) })) };
        // Publish each success before the next request so retry never recreates a saved question.
        if (entry.key === currentKey.current) replaceDraft(saved);
        else storeEntries(parkedRef.current.map(item => item.key === entry.key
          ? { ...item, value: saved, baseline: snapshot(saved), history: [] } : item));
      }
      await refresh();
      setAppendRegionMode(false); setNotice(all ? "全部题目已保存" : "题目已保存"); return true;
    } catch (failure) { setError((failure as Error).message); await refresh(); throw failure; }
    finally { setBusy(false); }
  }
  const clearUnsaved = useUnsavedDraft({ label: "题目草稿", dirty: dirtyCount > 0, busy: busy || uploading,
    save: () => save(true), discard: discardAll });
  function guarded(action: () => void) { leave(action); }
  function selectDocument(id: string) {
    if (id === documentId) { setView("reader"); onDocumentChange?.(id); return; }
    guarded(() => {
      clearUnsaved(); resetDrafts(); setDocumentId(id); remember(selectionKey, id);
      setView("reader"); setSelecting(false); setError(""); setNotice(""); restoring.current = "";
      onDocumentChange?.(id);
    });
  }
  function locate(region: StudyRegion) {
    setView("reader");
    requestAnimationFrame(() => {
      const block = blocks.data?.find(item => item.id === region.block_id);
      const node = reader.current?.querySelector<HTMLElement>(`[data-study-block="${region.block_id}"]`);
      if (node && reader.current) {
        reader.current.scrollTop += node.getBoundingClientRect().top - reader.current.getBoundingClientRect().top - 12;
        if (region.bbox) reader.current.scrollTop += region.bbox[1] * (node.querySelector(".study-page-canvas")?.getBoundingClientRect().height || 0);
        node.classList.remove("study-located"); void node.offsetWidth; node.classList.add("study-located");
      }
      if (block?.page) { setPage(block.page); setPageInput(String(block.page)); }
    });
  }
  function openQuestion(question: StudyQuestion, index: number) {
    if (busy) return;
    if (question.id === draftRef.current.id) return;
    const existing = parkedRef.current.find(entry => entry.value.id === question.id);
    if (existing) { activateEntry(existing.key); if (existing.value.regions[0]) locate(existing.value.regions[0]); return; }
    parkCurrent();
    currentKey.current = question.id;
    currentOrder.current = ++nextOrder.current;
    const next: Draft = { id: question.id, title: question.title, number: question.number,
      regions: question.regions.map(({ block_id, bbox, start, end, selection_group }) => ({ block_id, ...(selection_group ? { selection_group } : {}), ...(bbox ? { bbox } : { start, end }) })) };
    replaceDraft(next); setRightOpen(true);
    setNotice(question.title || `题目 ${index + 1}`);
    if (next.regions[0]) locate(next.regions[0]);
  }
  function newQuestion() {
    startFreshDraft(); setSelecting(true); setRightOpen(true); setView("reader"); setNotice("");
  }
  useEffect(() => {
    if (!active || !requestedDocumentId || requestedDocumentId === documentId) return;
    clearUnsaved(); resetDrafts(); setDocumentId(requestedDocumentId); remember(selectionKey, requestedDocumentId);
    setView("reader"); setSelecting(false); setError(""); setNotice(""); restoring.current = "";
  }, [active, requestedDocumentId]);
  useEffect(() => {
    if (!active || requestedDocumentId) return;
    if (documents.data && !documents.data.some(item => item.id === documentId && !item.deleted)) {
      const first = documents.data.find(item => !item.deleted);
      if (first) { setDocumentId(first.id); remember(selectionKey, first.id); setView("reader"); }
      else { setDocumentId(""); resetDrafts(); }
    }
  }, [active, requestedDocumentId, documents.data, documentId]);
  useEffect(() => {
    if (!active || !document || document.deleted || !courseId
      || (requestedDocumentId && requestedDocumentId !== document.id)) return;
    if (!requestedDocumentId) onDocumentChange?.(document.id);
  }, [active, document, courseId, requestedDocumentId, onDocumentChange]);
  useLayoutEffect(() => {
    if (!active || !document || !blocks.data || !questions.data || restoring.current === document.id) return;
    restoring.current = document.id;
    const reading = readingStates.current.get(document.id)
      || (initialReading.current.id === document.id ? initialReading.current.value : undefined) || document.reading;
    setZoom(reading.zoom || 1); setPage(reading.page || 1); setPageInput(String(reading.page || 1));
    setLeftOpen(reading.left_open !== false); setRightOpen(reading.right_open ?? questions.data.some(item => !item.deleted));
    const question = questions.data.find(item => item.id === reading.question_id && !item.deleted);
    storeEntries([]); currentOrder.current = 0; nextOrder.current = 0;
    currentKey.current = question?.id || crypto.randomUUID();
    if (question) replaceDraft({ id: question.id, title: question.title, number: question.number,
      regions: question.regions.map(({ block_id, bbox, start, end, selection_group }) => ({ block_id, ...(selection_group ? { selection_group } : {}), ...(bbox ? { bbox } : { start, end }) })) });
    else replaceDraft(emptyDraft());
    // Apply scroll after the restored zoom has changed the page dimensions.
    requestAnimationFrame(() => {
      if (selectedDocument.current === document.id && reader.current) reader.current.scrollTop = reading.scroll_top || 0;
    });
  }, [active, document?.id, blocks.data, questions.data]);
  if (document && restoring.current === document.id) {
    const previous = readingStates.current.get(document.id);
    readingStates.current.set(document.id, { zoom, page, question_id: draft.id || null,
      left_open: leftOpen, right_open: rightOpen, scroll_top: previous?.scroll_top ?? document.reading.scroll_top ?? 0 });
  }
  useEffect(() => {
    if (!active || !readable || !blocks.data) return;
    const panel = reader.current;
    let timer: ReturnType<typeof setTimeout>;
    function persist() {
      const value = readingStates.current.get(documentId);
      if (!value) return;
      const latestDocuments = client.getQueryData<typeof documents.data>(documentsKey);
      if (!latestDocuments?.some(item => item.id === documentId && !item.deleted)) return;
      client.setQueryData<typeof documents.data>(documentsKey, items =>
        items?.map(item => item.id === documentId && !item.deleted ? { ...item, reading: value } : item));
      const pending = readingWrites.current.get(documentId) || Promise.resolve();
      const next = pending.catch(() => {}).then(() => saveStudyReading(documentId, value));
      readingWrites.current.set(documentId, next);
      void next.catch(() => { setError("阅读位置保存失败，题目内容不受影响"); });
    }
    function scroll() {
      if (!panel?.clientHeight) return;
      const value = readingStates.current.get(documentId);
      if (value) {
        const visible = [...panel?.querySelectorAll<HTMLElement>(".study-source-page") || []].find(node =>
          node.getBoundingClientRect().bottom > (panel?.getBoundingClientRect().top || 0) + 50);
        const currentBlock = blocks.data?.find(item => item.id === visible?.dataset.studyBlock);
        if (currentBlock?.page && !currentBlock.supplement) { setPage(currentBlock.page); setPageInput(String(currentBlock.page)); }
        readingStates.current.set(documentId, { ...value, scroll_top: panel?.scrollTop || 0 });
      }
      clearTimeout(timer); timer = setTimeout(persist, 400);
    }
    panel?.addEventListener("scroll", scroll, { passive: true });
    timer = setTimeout(persist, 500);
    return () => { clearTimeout(timer); persist(); panel?.removeEventListener("scroll", scroll); };
  }, [active, documentId, readable, !!blocks.data, zoom, page, draft.id, leftOpen, rightOpen]);
  useEffect(() => {
    if (!active) return;
    const key = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z" || event.shiftKey || event.altKey) return;
      const target = event.target as HTMLElement;
      if (target.closest("input,textarea,[contenteditable=true],[role=dialog]")) return;
      if (history.length) { event.preventDefault(); undo(); }
    };
    window.addEventListener("keydown", key); return () => window.removeEventListener("keydown", key);
  }, [active, history]);

  async function importFiles(selected: File[]) {
    if (!selected.length || uploading) return;
    setUploading(true); setImportErrors([]); setError("");
    const failures: string[] = []; let first = "";
    for (let index = 0; index < selected.length; index++) {
      setUploadProgress({ name: selected[index].name, index: index + 1, count: selected.length });
      try { const result = await uploadStudyDocument(selected[index], courseId); first ||= result.id; }
      catch (failure) { failures.push(`${selected[index].name}：${(failure as Error).message}`); }
    }
    await refresh(); setUploading(false); setUploadProgress(null); setImportErrors(failures);
    if (first) selectDocument(first);
  }
  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = [...event.target.files || []]; event.target.value = "";
    guarded(() => { void importFiles(selected); });
  }
  function toggleMarkdown(block: StudyBlock, event: { shiftKey: boolean }) {
    if (!selecting || busy) return;
    const markdown = (blocks.data || []).filter(item => item.kind === "markdown");
    const index = markdown.findIndex(item => item.id === block.id);
    const selected = event.shiftKey && markdownAnchor.current >= 0
      ? markdown.slice(Math.min(index, markdownAnchor.current), Math.max(index, markdownAnchor.current) + 1) : [block];
    const exists = draft.regions.some(region => region.block_id === block.id);
    const ids = new Set(selected.map(item => item.id));
    const next = exists && !event.shiftKey ? draft.regions.filter(region => !ids.has(region.block_id))
      : [...draft.regions.filter(region => !ids.has(region.block_id)), ...selected.map(item => ({ block_id: item.id, start: 0, end: item.text?.length || 0 }))];
    next.sort((a, b) => (blocks.data?.find(item => item.id === a.block_id)?.ordinal || 0) - (blocks.data?.find(item => item.id === b.block_id)?.ordinal || 0));
    updateDraft({ ...draft, regions: next }); markdownAnchor.current = index;
  }
  async function changeDeleted(type: "study-documents" | "study-questions", id: string, deleted: boolean) {
    setBusy(true); setError("");
    try {
      await studyWrite(`/api/${type}/${id}${deleted ? "/restore" : ""}`, deleted ? "POST" : "DELETE");
      if (type === "study-questions") {
        storeEntries(parkedRef.current.filter(entry => entry.value.id !== id));
        if (id === draft.id) {
          currentKey.current = crypto.randomUUID(); currentOrder.current = ++nextOrder.current;
          replaceDraft(emptyDraft());
        }
      }
      await refresh();
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  function jumpPage(value: number) {
    const next = Math.max(1, Math.min(document?.page_count || 1, value));
    const block = blocks.data?.find(item => item.page === next && !item.supplement);
    if (block) locate({ block_id: block.id });
  }
  const questionList = (questions.data || []).filter(item => item.deleted === recycle);
  const blockError = documents.error || blocks.error || questions.error;
  const activeQuestions = (questions.data || []).filter(item => !item.deleted);
  const localEntries = [...parked, { key: currentKey.current, order: currentOrder.current, value: draft, baseline, history }]
    .sort((a, b) => a.order - b.order);
  const unsavedEntries = localEntries.filter(entry => !entry.value.id
    && (entry.value.regions.length || !same(entry.value, entry.baseline)));
  const draftNumber = draft.id ? Math.max(1, activeQuestions.findIndex(item => item.id === draft.id) + 1)
    : activeQuestions.length + Math.max(0, unsavedEntries.findIndex(entry => entry.key === currentKey.current)) + 1;
  const editableIndices = selecting && !appendRegionMode ? draft.regions.map((_, index) => index) : [];
  const editing = selecting || isDirty;
  const savedQuestion = activeQuestions.find(item => item.id === draft.id && item.document_id === documentId);
  const questionTitle = draft.title || `题目 ${draftNumber}`;
  const hasQuestionImages = draft.regions.some(region => blocks.data?.some(block =>
    block.id === region.block_id && (block.kind === "pdf_page" || block.kind === "image")));
  const continuousPreview = document?.format === "markdown" || draft.regions.some((region, index, values) =>
    region.selection_group && values.some((other, position) => position !== index && other.selection_group === region.selection_group));
  const regionPreviews = draft.regions.map((region, index) => {
    const block = blocks.data?.find(item => item.id === region.block_id);
    if (!block) return null;
    return <div className={`study-region-preview${continuousPreview ? " source-only" : ""}${selectedIndex === index ? " selected" : ""}`} key={`${region.block_id}:${index}`}>
      <header><div className="study-region-heading"><strong>区域 {index + 1}</strong><button className="text-button" title={studySourceLabel(block)} aria-label={`定位选区 ${index + 1}`}
        onClick={() => { setSelectedIndex(index); locate(region); }}><MapPin size={13} />{block.kind === "markdown" ? `片段 ${index + 1}` : studySourceLabel(block)}</button></div>
        {editing && block.kind !== "markdown" && <>
          <button className="icon-button" title="调整选框" aria-label={`调整选框 ${index + 1}`} disabled={busy}
            aria-pressed={editableIndex === index} onClick={() => {
              setSelectedIndex(index); setEditableIndex(value => value === index ? -1 : index);
              setAppendRegionMode(false); setSelecting(true); locate(region);
            }}><Crop size={13} /></button>
          <button className="icon-button" title="上移区域" aria-label={`上移区域 ${index + 1}`} disabled={!index || busy} onClick={() => {
            const next = [...draft.regions]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; updateDraft({ ...draft, regions: next }); setEditableIndex(-1);
          }}><ArrowUp size={13} /></button>
          <button className="icon-button" title="下移区域" aria-label={`下移区域 ${index + 1}`} disabled={index === draft.regions.length - 1 || busy} onClick={() => {
            const next = [...draft.regions]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; updateDraft({ ...draft, regions: next }); setEditableIndex(-1);
          }}><ArrowDown size={13} /></button></>}
        {editing && <button className="icon-button" title="移除区域" aria-label={`移除区域 ${index + 1}`} disabled={busy}
          onClick={() => { updateDraft({ ...draft, regions: draft.regions.filter((_, position) => position !== index) }); setEditableIndex(-1); }}><X size={13} /></button>}
      </header>
      {!continuousPreview && <button className="study-region-zoom" aria-label={`放大选区 ${index + 1}`} onClick={() => setPreviewOpen(true)}>
        <img src={studyRegionUrl(documentId, region)} alt={`题目选区 ${index + 1}`} /></button>}
    </div>;
  });
  const otherRegions = [
    ...activeQuestions.filter(question => question.id !== draft.id && !parked.some(entry => entry.value.id === question.id))
      .map((question, index) => ({ key: question.id, value: question, label: `题目 ${activeQuestions.indexOf(question) + 1}`, index })),
    ...parked.map(entry => ({ ...entry, label: entry.value.id
      ? `题目 ${activeQuestions.findIndex(question => question.id === entry.value.id) + 1}`
      : `题目 ${activeQuestions.length + unsavedEntries.findIndex(item => item.key === entry.key) + 1}` })),
  ];
  function openOtherRegion(key: string) {
    if (parkedRef.current.some(entry => entry.key === key)) activateEntry(key);
    else {
      const question = activeQuestions.find(item => item.id === key);
      if (question) openQuestion(question, activeQuestions.indexOf(question));
    }
  }
  const questionIndex = <div className="study-question-index">
    {!!unsavedEntries.length && !recycle && <>
      <div className="study-question-list-heading"><strong>题目草稿</strong><span>{unsavedEntries.length}</span></div>
      <div className="study-question-index-list">{unsavedEntries.map((entry, index) =>
        <div key={entry.key} className={`study-question-row study-question-draft-row${entry.key === currentKey.current ? " selected" : ""}`}>
          <button className="study-question-open" disabled={busy} onClick={() => { activateEntry(entry.key); setRightOpen(true); }}>
            <strong>{entry.value.title || `题目 ${activeQuestions.length + index + 1}`}</strong>
            <small>{entry.value.regions.length} 个区域 · 未保存</small>
          </button>
        </div>)}</div>
    </>}
    <div className="study-question-list-heading"><strong>{recycle ? "已移除题目" : "已保存题目"}</strong><span>{questionList.length}</span></div>
    <div className="study-question-index-list">{questionList.map((question, index) => <div key={question.id} className={`study-question-row${draft.id === question.id ? " selected" : ""}`}>
      <button className="study-question-open" disabled={question.deleted || busy} onClick={() => openQuestion(question, index)}><strong>{question.number && `${question.number} · `}{localEntries.find(entry => entry.value.id === question.id)?.value.title || question.title || `题目 ${index + 1}`}
        {localEntries.some(entry => entry.value.id === question.id && !same(entry.value, entry.baseline)) && " · 未保存"}</strong>
        <small>{question.regions.map(region => region.block.kind === "markdown" ? "Markdown" : studySourceLabel(region.block))
          .filter((label, pos, labels) => labels.indexOf(label) === pos).join("、")}</small></button>
      <button className="icon-button" title={question.deleted ? "恢复题目" : "移除题目"} aria-label={`${question.deleted ? "恢复" : "移除"}题目 ${index + 1}`}
        disabled={busy} onClick={() => guarded(() => { void changeDeleted("study-questions", question.id, question.deleted); })}>
        {question.deleted ? <RotateCcw size={14} /> : <Trash2 size={14} />}</button></div>)}</div>
  </div>;

  return <main className="study-materials-page" data-view={view} onDragOver={event => {
    if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
  }} onDrop={event => {
    if (event.dataTransfer.files.length) { event.preventDefault(); const selected = [...event.dataTransfer.files]; guarded(() => { void importFiles(selected); }); }
  }}>
    <header className="study-heading"><div>
      {!courseId && <h1 tabIndex={-1}>学习资料</h1>}<span className="study-muted">{documents.data?.filter(item => !item.deleted).length || 0} 份资料</span></div>
      <button className="primary-button" disabled={uploading || busy} onClick={() => files.current?.click()}><Upload size={17} />导入资料</button>
      <input ref={files} hidden type="file" multiple accept=".pdf,.md,.markdown,.jpg,.jpeg,.png,.webp" onChange={chooseFiles} aria-label="选择学习资料" />
    </header>
    {uploadProgress && <div className="study-import-progress" role="status"><LoaderCircle size={16} className="spin" />
      <span>正在导入 {uploadProgress.index}/{uploadProgress.count}：{uploadProgress.name}</span>
      <progress value={uploadProgress.index - 1} max={uploadProgress.count} /></div>}
    {(error || blockError) && <div className="study-error" role="alert"><span>{error || (blockError as Error).message}</span>
      <button className="text-button" onClick={() => { setError(""); void refresh(); }}>重新读取</button></div>}
    {requestedDocumentId && documents.isSuccess && (!document || document.deleted) && <div className="study-error" role="alert">
      这份资料已移除或已不在当前课程中，请从文件列表选择其他资料。
    </div>}
    {!!importErrors.length && <div className="study-error" role="alert"><div>{importErrors.map(message => <p key={message}>{message}</p>)}</div>
      <button className="icon-button" aria-label="关闭导入错误" title="关闭" onClick={() => setImportErrors([])}><X size={16} /></button></div>}
    <div className={`study-workspace${leftOpen ? "" : " left-closed"}${rightOpen ? "" : " right-closed"}`}>
      <aside className="study-files" aria-label="学习资料文件">
        <div className="study-pane-heading"><strong>资料</strong><button className="icon-button study-desktop-toggle" title="收起文件列表" aria-label="收起文件列表" onClick={() => setLeftOpen(false)}><PanelLeftClose size={16} /></button></div>
        <label className="study-file-search"><input aria-label="搜索资料" placeholder="搜索资料" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <div className="study-files-scroll">
          {documents.isLoading && <div className="study-empty" role="status"><LoaderCircle className="spin" size={20} />正在读取…</div>}
          {available.map(item => <div className={`study-file-row${item.id === documentId ? " selected" : ""}`} key={item.id}>
            {renamingDocument === item.id ? <form className="study-file-rename" onSubmit={async event => {
              event.preventDefault(); setBusy(true); setError("");
              try { await studyWrite(`/api/study-documents/${item.id}`, "PUT", { name: documentName }); setRenamingDocument(""); await refresh(); }
              catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
            }}><input autoFocus aria-label="资料名称" required maxLength={240} value={documentName} onChange={event => setDocumentName(event.target.value)} />
              <button className="icon-button" disabled={busy} title="保存资料名称" aria-label="保存资料名称"><Check size={14} /></button>
              <button className="icon-button" type="button" title="取消改名" aria-label="取消改名" onClick={() => setRenamingDocument("")}><X size={14} /></button></form>
              : <><button className="study-file-open" title={item.name} disabled={item.deleted} onClick={() => selectDocument(item.id)}><FileText size={17} /><span><strong>{item.name}</strong>
                <small>{item.format === "pdf" ? `${item.page_count} 页` : item.format === "image" ? "图片" : "Markdown"} · {item.question_count} 道题</small></span></button>
                <button className="icon-button study-file-rename-button" title="修改资料名称" aria-label={`修改名称 ${item.name}`} disabled={busy} onClick={() => { setRenamingDocument(item.id); setDocumentName(item.name); }}><Pencil size={13} /></button></>}
            <button className="icon-button" title={item.deleted ? "恢复资料" : "移除资料"} aria-label={`${item.deleted ? "恢复" : "移除"} ${item.name}`}
              disabled={busy || uploading} onClick={() => guarded(() => { void changeDeleted("study-documents", item.id, item.deleted); })}>
              {item.deleted ? <RotateCcw size={14} /> : <Trash2 size={14} />}</button></div>)}
          {!documents.isLoading && !available.length && <div className="study-empty"><FileText size={26} /><span>{query ? "没有匹配的资料" : recycle ? "没有已移除资料" : "暂无资料"}</span>
            {query ? <button className="text-button" onClick={() => setQuery("")}>清除搜索</button>
              : !recycle && <button className="text-button" onClick={() => files.current?.click()}><Upload size={16} />导入资料</button>}</div>}
        </div>
        <label className="study-recycle"><input type="checkbox" checked={recycle} onChange={event => setRecycle(event.target.checked)} /><Trash2 size={14} />已移除内容</label>
      </aside>
      <section className="study-reader" aria-label="资料原文">
        <div className="study-reader-toolbar">
          <button className="icon-button study-mobile-only" title="返回资料" aria-label="返回资料" onClick={() => setView("files")}><ArrowLeft size={16} /></button>
          {!leftOpen && <button className="icon-button study-desktop-toggle" title="展开文件列表" aria-label="展开文件列表" onClick={() => setLeftOpen(true)}><PanelLeftOpen size={16} /></button>}
          <strong className="study-current-name" title={document?.name}>{document?.name || "资料原文"}</strong>
          <div className="study-mode" aria-label="阅读模式"><button aria-pressed={!selecting} disabled={!readable || busy} onClick={() => { setSelecting(false); setEditableIndex(-1); setAppendRegionMode(false); }}><BookOpen size={15} />阅读</button>
            <button aria-pressed={selecting} disabled={!readable || busy} onClick={() => { setSelecting(true); setRightOpen(true); }}><Crop size={15} />选题</button></div>
          <button className="icon-button" title={rightOpen ? "收起题目侧栏" : "展开题目侧栏"} aria-label="题目侧栏" onClick={() => {
            setRightOpen(value => window.innerWidth <= 950 || !value); setView("questions");
          }}>
            {rightOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}</button>
        </div>
        {readable && <div className="study-navigation-toolbar">
          {document?.format === "pdf" && <label className="study-snap-toggle" title="按本页全部文字的最大宽度向外扩展；按住 Alt 可临时自由框选">
            <input type="checkbox" checked={snapText} onChange={event => setSnapText(event.target.checked)} disabled={busy} />吸附文字边界</label>}
          {document?.format !== "markdown" && <><button className="icon-button" title="上一页" aria-label="上一页" disabled={page <= 1} onClick={() => jumpPage(page - 1)}><ChevronLeft size={15} /></button>
            <form onSubmit={event => { event.preventDefault(); jumpPage(Number(pageInput) || 1); }}><input aria-label="页码" inputMode="numeric" value={pageInput} onChange={event => setPageInput(event.target.value)} onBlur={() => jumpPage(Number(pageInput) || 1)} /><span>/ {document?.page_count}</span></form>
            <button className="icon-button" title="下一页" aria-label="下一页" disabled={page >= (document?.page_count || 1)} onClick={() => jumpPage(page + 1)}><ChevronRight size={15} /></button></>}
          <button className="icon-button" title="缩小" aria-label="缩小" disabled={zoom <= .5} onClick={() => setZoom(value => Math.max(.5, Math.round((value - .1) * 10) / 10))}><ZoomOut size={15} /></button>
          <span className="study-zoom">{Math.round(zoom * 100)}%</span>
          <button className="icon-button" title="放大" aria-label="放大" disabled={zoom >= 3} onClick={() => setZoom(value => Math.min(3, Math.round((value + .1) * 10) / 10))}><ZoomIn size={15} /></button>
          <button className="icon-button" title="适合宽度" aria-label="适合宽度" onClick={() => setZoom(1)}><ScanLine size={15} /></button>
          <button className="icon-button study-desktop-toggle" title={!leftOpen && !rightOpen ? "退出专注阅读" : "专注阅读"}
            aria-label={!leftOpen && !rightOpen ? "退出专注阅读" : "专注阅读"} aria-pressed={!leftOpen && !rightOpen} onClick={() => {
              if (!leftOpen && !rightOpen) { setLeftOpen(focusPanes.current.left); setRightOpen(focusPanes.current.right); }
              else { focusPanes.current = { left: leftOpen, right: rightOpen }; setLeftOpen(false); setRightOpen(false); }
            }}>{!leftOpen && !rightOpen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
          <button className="text-button study-mobile-only" onClick={() => setView("questions")}><List size={16} />{editing ? `已选 ${draft.regions.length}` : `题目 ${questions.data?.filter(item => !item.deleted).length || 0}`}</button>
        </div>}
        <div ref={reader} className="study-reader-scroll">
          {!readable ? <div className="study-empty study-reader-empty"><BookOpen size={38} /><h2>打开一份学习资料</h2>
            <button className="primary-button" disabled={uploading} onClick={() => files.current?.click()}><Upload size={17} />导入资料</button></div>
          : blocks.isLoading ? <div className="study-empty" role="status"><LoaderCircle className="spin" size={20} />正在读取原文…</div>
          : <div ref={markdownSurface} className={`study-document-content${document?.format === "markdown" ? " study-markdown-surface" : ""}${document?.format === "markdown" && selecting ? " selecting" : ""}`}
            style={document?.format === "markdown" ? { fontSize: `${zoom * 16}px` } : undefined}
            onPointerDown={markdownBox.onPointerDown} onPointerMove={event => { markdownBox.onPointerMove(event); pageBox.onPointerMove(event); }}
            onPointerUp={event => { markdownBox.onPointerUp(event); void pageBox.onPointerUp(event); }}
            onPointerCancel={() => { markdownBox.onPointerCancel(); pageBox.onPointerCancel(); }}
            onLostPointerCapture={event => {
              if (event.target === event.currentTarget) { markdownBox.onLostPointerCapture(); pageBox.onLostPointerCapture(); }
            }}>
            {blocks.data?.map(block => block.kind === "markdown" ? <section key={block.id} data-study-block={block.id}
              className={`study-markdown-block${selecting && (draft.regions.some(region => region.block_id === block.id) || markdownBox.targetIds.includes(block.id)) ? " is-selected" : ""}${selecting && otherRegions.some(entry => entry.value.regions.some(region => region.block_id === block.id)) ? " has-question" : ""}`}>
              {selecting && <div className="study-block-check" title={studySourceLabel(block)} onClick={event => toggleMarkdown(block, event)}>
                <input type="checkbox" aria-label={`选择${studySourceLabel(block)}`} checked={draft.regions.some(region => region.block_id === block.id) || markdownBox.targetIds.includes(block.id)}
                  onChange={() => {}} onKeyDown={event => {
                    if (event.key === " ") { event.preventDefault(); event.stopPropagation(); toggleMarkdown(block, event); }
                  }} />
                <span>{studySourceLabel(block)}</span></div>}
              <StudyMarkdown text={block.text || ""} />
            </section> : <StudyPageCanvas key={block.id} documentId={documentId} block={block} zoom={zoom} selecting={selecting && !busy}
              questionLabel={`题目 ${draftNumber}`}
              regions={draft.regions.map((region, index) => ({ ...region, index })).filter(region => region.block_id === block.id)}
              savedRegions={otherRegions.flatMap(entry => entry.value.regions.filter(region => region.block_id === block.id)
                .map(region => ({ ...region, questionKey: entry.key, label: entry.label })))}
              onOpenRegion={openOtherRegion}
              selectedIndex={selectedIndex} editableIndex={editableIndex} editableIndices={editableIndices} scope={scope} snap={snapText}
              drawing={pageBox.regions.find(region => region.block_id === block.id)?.bbox} onDrawStart={pageBox.start}
              onSelect={index => { setSelectedIndex(index); setEditableIndex(index); }} onCommit={commitGesture} onError={setError}
              onAdd={bbox => {
                if (!appendRegionMode && draftRef.current.regions.length) startFreshDraft();
                updateDraft({ ...draftRef.current, regions: [...draftRef.current.regions, { block_id: block.id, bbox }] });
                const index = draftRef.current.regions.length - 1;
                setSelectedIndex(index); setEditableIndex(index); setRightOpen(true);
                setAppendRegionMode(false); }}
              onChange={(index, bbox) => updateDraft({ ...draftRef.current, regions: draftRef.current.regions.map((region, position) => position === index ? { ...region, bbox } : region) }, false)} />)}
            {!blocks.data?.length && <div className="study-empty">资料没有可读内容</div>}
            {markdownBox.box && <div className="study-markdown-selection-box" style={markdownBox.box} aria-hidden="true" />}
          </div>}
        </div>
        {editing && !!draft.regions.length && <div className="study-mobile-draft-bar">
          <span>{draft.regions.length} 个区域{isDirty ? " · 未保存" : ""}</span>
          <button className="primary-button" onClick={() => { setRightOpen(true); setView("questions"); }}><Check size={16} />预览并保存</button>
        </div>}
      </section>
      <aside className="study-question-pane" aria-label="题目与选区">
        <div className="study-pane-heading"><button className="icon-button study-mobile-only" title="返回原文" aria-label="返回原文" onClick={() => setView("reader")}><ArrowLeft size={16} /></button>
          <strong>{selecting || isDirty ? "题目草稿" : "题目"}</strong><button className="icon-button" title="新建题目" aria-label="新建题目" disabled={!readable || busy} onClick={newQuestion}><Plus size={17} /></button></div>
        {questionIndex}
        <div className="study-question-scroll">
          {(draft.regions.length > 0 || editing) && <div className="study-draft">
            <div className="study-draft-heading"><span>{editing ? draft.id ? "编辑题目" : `题目 ${draftNumber}` : "当前题目"}{isDirty && " · 未保存"}</span>
              <div className="study-draft-heading-actions">
                {!!draft.regions.length && !hasQuestionImages && <button className="icon-button" type="button"
                  title="放大题目预览" aria-label="放大题目预览" aria-haspopup="dialog" onClick={() => setPreviewOpen(true)}>
                  <Maximize2 size={15} /></button>}
                {!selecting && <button className="icon-button" title="编辑题目" aria-label="编辑题目" onClick={() => {
                  setAppendRegionMode(false); setSelecting(true);
                }}><Pencil size={15} /></button>}
              </div>
            </div>
            {!editing && <div className="study-question-summary"><h2 className="study-question-title">{questionTitle}</h2>
              <span>{draft.number && `原题号 ${draft.number} · `}{draft.regions.length} 个区域</span></div>}
            {editing && <><input aria-label="题目标题" placeholder={`题目 ${draftNumber}`} value={draft.title} maxLength={180} disabled={busy}
              onChange={event => updateDraft({ ...draft, title: event.target.value })} />
            <input aria-label="原题号" placeholder="题号（选填）" value={draft.number} maxLength={120} disabled={busy}
              onChange={event => updateDraft({ ...draft, number: event.target.value })} />
            <div className="study-draft-tools"><button className="text-button" disabled={busy || !draft.regions.length} aria-pressed={appendRegionMode} onClick={() => {
              setEditableIndex(-1); setAppendRegionMode(value => !value); setSelecting(true); setView("reader");
            }}><Plus size={15} />{appendRegionMode ? "取消添加区域" : "添加另一区域"}</button>
              <button className="icon-button" title="撤销选区操作" aria-label="撤销选区操作" disabled={!history.length || busy} onClick={undo}><Undo2 size={15} /></button></div>
            </>}
            {!draft.regions.length && <p className="study-muted study-no-region">尚未选择原文范围</p>}
            {continuousPreview ? <>
              <StudyQuestionContent documentId={documentId} regions={draft.regions} blocks={blocks.data || []}
                onPreview={() => setPreviewOpen(true)} />
              {!!draft.regions.length && <details className="study-source-management"><summary>来源与选区</summary>{regionPreviews}</details>}
            </> : regionPreviews}
            {editing && <button className="text-button" disabled={!readable || busy} onClick={() => attachment.current?.click()}><ImagePlus size={15} />补充题图</button>}
          </div>}
          <input ref={attachment} hidden type="file" accept=".jpg,.jpeg,.png,.webp" aria-label="选择补充题图" onChange={async event => {
            const file = event.target.files?.[0]; event.target.value = ""; if (!file) return;
            setBusy(true); setError("");
            try { const block = await uploadStudyAttachment(documentId, file); await refresh();
              updateDraft({ ...draftRef.current, regions: [...draftRef.current.regions, { block_id: block.id, bbox: [0, 0, 1, 1] }] });
            } catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
          }} />
          {notice && <p className="study-saved-notice" role="status"><Check size={14} />{notice}</p>}
          {!questionList.length && !selecting && !draft.regions.length && <div className="study-empty"><Crop size={26} /><span>尚未保存题目</span>
            <button className="text-button" disabled={!readable} onClick={newQuestion}><Plus size={15} />选取一道题</button></div>}
        </div>
        {readable && savedQuestion && !editing && !busy && !uploading && <DshQuestionActions
          questionId={savedQuestion.id} contentVersion={savedQuestion.version} scope={scope}
          active={active && rightOpen} />}
        {dirtyCount > 1 && <div className="study-batch-actions"><span>{dirtyCount} 道题未保存</span>
          <button className="text-button" disabled={busy} onClick={() => { void save(true).catch(() => {}); }}><Save size={15} />全部保存</button></div>}
        {editing && <div className="study-draft-actions"><span className="study-muted">{draft.regions.length} 个区域</span>
          <button className="primary-button" disabled={busy || !draft.regions.length || !isDirty}
            onClick={() => { void save().catch(() => {}); }}>{busy ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}保存题目</button>
          <button className="text-button" disabled={busy} onClick={() => { replaceDraft(snapshot(baseline)); setSelecting(false); }}>取消</button></div>}
      </aside>
    </div>
    {previewOpen && <StudyQuestionPreview documentId={documentId} title={questionTitle} regions={draft.regions}
      blocks={blocks.data || []} onClose={() => setPreviewOpen(false)} />}
  </main>;
}
