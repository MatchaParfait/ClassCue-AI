import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Check, FileText, Folder, FolderPlus, LoaderCircle, Video, X } from "lucide-react";
import { createStudyCourse, getCourseForFolder, getCourseVideoQuestions, getStudyCourseFolders,
  getStudyCourses, getStudyDocuments, studyWrite, type StudyCourseFolder } from "../api/studyMaterials";
import { questionCaptureUrl } from "../api/questions";
import { DshQuestionActions } from "../components/DshQuestionActions";
import { useUnsavedLeave } from "../components/UnsavedChanges";
import { StudyMaterialsPage } from "./StudyMaterialsPage";
import { formatTime } from "../utils";
import "./StudyMaterialsPage.css";
import "./StudyCoursesPage.css";

function folderLabel(folder: StudyCourseFolder, folders: StudyCourseFolder[]) {
  const names = [folder.name], seen = new Set([folder.id]);
  let parent = folders.find(row => row.id === folder.parent_id);
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id); names.unshift(parent.name); parent = folders.find(row => row.id === parent!.parent_id);
  }
  return names.join(" / ");
}

export function StudyCoursesPage({ active = true, scope = "default", libraryId = "" }: { active?: boolean; scope?: string; libraryId?: string }) {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const courseId = params.get("course") || "";
  const incomingFolder = Number(params.get("folder")) || null;
  const unassigned = params.get("view") === "unassigned";
  const tab = params.get("tab") === "video" ? "video" : "documents";
  const cache = useQueryClient();
  const leave = useUnsavedLeave();
  const [creating, setCreating] = useState(false), [folder, setFolder] = useState(""), [name, setName] = useState("");
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");
  const [destination, setDestination] = useState(""), [chosen, setChosen] = useState<string[]>([]);
  const [folderRetry, setFolderRetry] = useState(0);
  const [rebinding, setRebinding] = useState(false), [bindingFolder, setBindingFolder] = useState("");
  const courses = useQuery({ queryKey: ["study-courses", scope], queryFn: getStudyCourses, enabled: active });
  const folders = useQuery({ queryKey: ["study-course-folders", scope], queryFn: getStudyCourseFolders, enabled: active });
  const legacy = useQuery({ queryKey: ["study-unassigned", scope], queryFn: () => getStudyDocuments(undefined, true), enabled: active });
  const examples = useQuery({ queryKey: ["study-video-questions", scope, courseId], queryFn: () => getCourseVideoQuestions(courseId),
    enabled: active && Boolean(courseId) && tab === "video", refetchOnWindowFocus: true });
  const course = courses.data?.find(row => row.id === courseId);
  const videoQuestion = examples.data?.find(row => row.id === selected) || examples.data?.find(row => !row.unavailable);
  const refresh = async () => { await Promise.all([
    cache.invalidateQueries({ queryKey: ["study-courses", scope] }),
    cache.invalidateQueries({ queryKey: ["study-course-folders", scope] }),
    cache.invalidateQueries({ queryKey: ["study-unassigned", scope] }),
  ]); };
  const navigateWithin = (values: Record<string, string>) =>
    setParams(values, { replace: true, state: location.state });
  const open = (id: string) => { setError(""); setSelected(""); setRebinding(false); navigateWithin({ course: id }); };
  useEffect(() => {
    if (!incomingFolder || !active) { setBusy(false); return; }
    let disposed = false;
    setError(""); setBusy(true);
    void (async () => {
      try {
        const current = await getCourseForFolder(incomingFolder) || await createStudyCourse({ folder_id: incomingFolder });
        await refresh();
        if (!disposed) navigateWithin({ course: current.id });
      } catch (failure) { if (!disposed) setError((failure as Error).message); }
      finally { if (!disposed) setBusy(false); }
    })();
    return () => { disposed = true; };
  }, [incomingFolder, active, folderRetry]);
  const failure = error || (courses.error || folders.error || legacy.error || (tab === "video" ? examples.error : null))?.message;
  async function retry() {
    setError("");
    if (incomingFolder) setFolderRetry(value => value + 1);
    await Promise.all([refresh(), ...(courseId && tab === "video" ? [examples.refetch()] : [])]);
  }
  const home = () => leave(() => { setError(""); setRebinding(false); navigateWithin({}); });
  return <div className="study-courses-page" data-layout={course ? tab : "courses"}>
    <header className="study-courses-heading">
      <div>{(courseId || unassigned) && <button className="icon-button" title="返回全部课程" aria-label="返回全部课程" onClick={home}><ArrowLeft size={18} /></button>}
        <Folder size={22} /><h1>{course?.name || (unassigned ? "待归属资料" : "学习资料")}</h1></div>
      {course && <button className="secondary-button" disabled={busy} onClick={() => {
        setBindingFolder(course.folder_id === null ? "" : String(course.folder_id)); setRebinding(true); setError("");
      }}><Folder size={16} />{course.folder_id === null ? "关联视频文件夹" : "更改关联"}</button>}
      {course?.folder_id && <Link className="secondary-button" to={`/library/folder/folder-${course.folder_id}`}><Video size={16} />课程视频</Link>}
      {!courseId && !unassigned && <button className="primary-button" disabled={busy} onClick={() => { setCreating(value => !value); setError(""); }}><FolderPlus size={17} />新建课程资料夹</button>}
    </header>
    {failure && <div className="study-error" role="alert">{failure}<button className="text-button" onClick={() => { void retry(); }}>重新读取</button></div>}
    {(courses.isPending || busy && incomingFolder) && <div className="study-empty" role="status"><LoaderCircle className="spin" size={20} />正在读取课程…</div>}
    {creating && !courseId && !unassigned && <form className="study-course-form" onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError("");
      try {
        const result = await createStudyCourse(folder ? { folder_id: Number(folder) } : { name: name.trim() });
        await refresh(); setCreating(false); setFolder(""); setName(""); open(result.id);
      } catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
    }}>
      <label>绑定课程视频文件夹<select aria-label="绑定课程视频文件夹" value={folder} disabled={busy} onChange={event => setFolder(event.target.value)}>
        <option value="">新建同名视频文件夹</option>
        {folders.data?.filter(row => row.parent_id === null && !row.course_id).map(row => <option key={row.id} value={row.id}>{folderLabel(row, folders.data!)}</option>)}
      </select></label>
      {!folder && <label>课程名称<input aria-label="课程名称" autoFocus required maxLength={240} value={name} disabled={busy} onChange={event => setName(event.target.value)} /></label>}
      <button className="primary-button" disabled={busy || !folder && !name.trim()}><Check size={16} />创建</button>
      <button type="button" className="icon-button" disabled={busy} title="取消创建" aria-label="取消创建" onClick={() => setCreating(false)}><X size={16} /></button>
    </form>}
    {course && rebinding && <form className="study-course-form" onSubmit={async event => {
      event.preventDefault();
      if (!bindingFolder || Number(bindingFolder) === course.folder_id) return;
      setBusy(true); setError("");
      try {
        await studyWrite(`/api/study-courses/${courseId}/folder`, "PUT", { folder_id: Number(bindingFolder) });
        await Promise.all([refresh(), cache.invalidateQueries({ queryKey: ["study-video-questions", scope, courseId] })]);
        setSelected(""); setRebinding(false);
      } catch (failure) { setError((failure as Error).message); } finally { setBusy(false); }
    }}>
      <label>课程视频文件夹<select aria-label="重新绑定视频文件夹" required value={bindingFolder} disabled={busy} onChange={event => setBindingFolder(event.target.value)}>
        <option value="">选择课程文件夹</option>
        {folders.data?.filter(row => (!row.course_id || row.course_id === courseId) &&
          (row.parent_id === null || row.id === course.folder_id)).map(row => <option key={row.id} value={row.id}>{folderLabel(row, folders.data!)}</option>)}
      </select></label>
      <button className="primary-button" disabled={busy || !bindingFolder || Number(bindingFolder) === course.folder_id}><Check size={16} />保存关联</button>
      <button type="button" className="icon-button" disabled={busy} title="取消更改关联" aria-label="取消更改关联" onClick={() => setRebinding(false)}><X size={16} /></button>
    </form>}
    {!courseId && !unassigned && !courses.isPending && <div className="study-course-list" aria-label="课程资料夹">
      {courses.data?.map(row => <button className="study-course-row" key={row.id} onClick={() => open(row.id)}>
        <Folder size={26} /><span><strong>{row.name}</strong><small>{row.document_count} 份资料 · {row.question_count} 道资料题{row.folder_id === null ? " · 视频文件夹已移除" : ""}</small></span>
      </button>)}
      {!!legacy.data?.length && <button className="study-course-row" onClick={() => navigateWithin({ view: "unassigned" })}><FileText size={26} /><span><strong>待归属资料</strong><small>{legacy.data.length} 份</small></span></button>}
      {!courses.data?.length && !legacy.data?.length && <div className="study-empty"><FolderPlus size={32} /><span>暂无课程资料夹</span></div>}
    </div>}
    {course && <nav className="study-course-tabs" aria-label="课程资料视图">
      <button aria-pressed={tab === "documents"} onClick={() => leave(() => navigateWithin({ course: courseId }))}><BookOpen size={16} />上传资料</button>
      <button aria-pressed={tab === "video"} onClick={() => leave(() => navigateWithin({ course: courseId, tab: "video" }))}><Video size={16} />视频例题</button>
    </nav>}
    {course && <div className="study-course-reader" hidden={tab !== "documents"}>
      <StudyMaterialsPage key={`${scope}:${courseId}`} scope={scope} courseId={courseId} active={active && tab === "documents"} />
    </div>}
    {course && tab === "video" && <div className="study-course-videos">
      <aside aria-label="课程视频例题">{examples.isPending && <p role="status">正在读取例题…</p>}
        {examples.data?.map(row => row.unavailable ? <Link key={row.marker_id} className="study-video-row"
          to={`/review/${row.video_id}?start_ms=${row.start_ms}&library_id=${encodeURIComponent(libraryId)}`}>{row.title}</Link>
          : <button key={row.marker_id} className={`study-video-row${row.id === videoQuestion?.id ? " selected" : ""}`}
          onClick={() => setSelected(row.id!)}><strong>{row.title}</strong><small>{row.source_name} · {formatTime(row.start_ms)}</small></button>)}
        {!examples.isPending && !examples.data?.length && <div className="study-empty"><Video size={28} />暂无带题图的视频例题</div>}
      </aside>
      <section aria-label="视频例题阅读">{videoQuestion && !videoQuestion.unavailable ? <>
        <header><h2>{videoQuestion.title}</h2><Link className="text-button" to={`/review/${videoQuestion.video_id}?start_ms=${videoQuestion.start_ms}&library_id=${encodeURIComponent(libraryId)}`}>返回视频 · {formatTime(videoQuestion.start_ms)}</Link></header>
        <img src={questionCaptureUrl(videoQuestion.marker_id, videoQuestion.capture_id)} alt={videoQuestion.title} />
        {videoQuestion.id && <DshQuestionActions key={videoQuestion.id} questionId={videoQuestion.id} contentVersion={videoQuestion.version} scope={scope} active={active} />}
      </> : <div className="study-empty"><BookOpen size={32} />选择一道视频例题</div>}</section>
    </div>}
    {courseId && !course && !courses.isPending && <div className="study-empty">课程资料夹不存在<button className="text-button" onClick={home}>返回全部课程</button></div>}
    {unassigned && <section className="study-unassigned">
      <form onSubmit={async event => {
        event.preventDefault(); setBusy(true); setError("");
        const pending = [...chosen];
        try {
          for (const id of pending) {
            await studyWrite(`/api/study-documents/${id}/course`, "PUT", { course_id: destination });
            setChosen(values => values.filter(value => value !== id));
          }
          await refresh();
        } catch (failure) { setError((failure as Error).message); await refresh(); } finally { setBusy(false); }
      }}>
        <select aria-label="资料目标课程" required value={destination} disabled={busy} onChange={event => setDestination(event.target.value)}>
          <option value="">选择课程资料夹</option>{courses.data?.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
        </select><button className="primary-button" disabled={busy || !destination || !chosen.length}><Folder size={16} />移入课程（{chosen.length}）</button>
      </form>
      {legacy.data?.map(row => <label key={row.id}><input type="checkbox" disabled={busy} checked={chosen.includes(row.id)}
        onChange={event => setChosen(values => event.target.checked ? [...values, row.id] : values.filter(id => id !== row.id))} /><FileText size={18} />{row.name}{row.deleted ? "（已移除）" : ""}</label>)}
      {!legacy.data?.length && <div className="study-empty">没有待归属资料</div>}
    </section>}
  </div>;
}
