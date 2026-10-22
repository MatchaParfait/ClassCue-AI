import { UnsavedChangesProvider } from "./components/UnsavedChanges";
import { useDragTextHints } from "./libraryPreferences";
import { useModalFocus } from "./hooks/useModalFocus";
import { useStartupLibraryView } from "./hooks/useStartupLibraryView";
import type { StartupLibraryView } from "./hooks/useDesktopAPI";
import { releaseStartupCover } from "./startupTransition";
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { Announcements, UpdateNotice } from "./components/Announcements";
import {
  AlertCircle,
  ArrowLeft,
  ArrowLeftRight,
  ArrowLeftToLine,
  ArrowRightToLine,
  ArrowDownAZ,
  CheckCircle2,
  ChevronRight,
  Clock3,
  Database,
  FileVideo,
  Folder,
  FolderInput,
  FolderOpen,
  FolderPlus,
  GripVertical,
  LoaderCircle,
  MoreHorizontal,
  Pencil,
  RotateCcw,
  Save,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { createBrowserRouter, RouterProvider, Link, Route, Routes, useLocation, useNavigate, useNavigationType, type Location } from "react-router-dom";
import { createLibraryFolder, deleteLibraryFolder, deleteVideo, getLatestBatch, getLatestRecycleBin, getLibrary, getPausedJobs, getVideos, getLibraryStatus, moveLibraryFolder, moveVideoToFolder, renameLibraryFolder, renameVideoFile, reorderLibrary, undoRecycleBin } from "./api/client";
import type { LibraryFolder } from "./api/client";
import { GlobalSearchProvider, GlobalSearchButton } from "./components/GlobalSearch";
import { FolderWindow } from "./components/FolderWindow";
import { VideoThumbnail } from "./components/VideoThumbnail";
import { RecycleConfirmDialog } from "./components/RecycleConfirmDialog";
import { ReviewTabs, ReviewTabsProvider, useReviewTabs } from "./components/ReviewTabs";
import { MarkerImportanceCounts } from "./components/MarkerImportanceCounts";
import { LibraryFileImport } from "./components/LibraryFileImport";
import { LibraryProcessing, canSelectForProcessing, DEFAULT_LIBRARY_PROCESS_FORM } from "./components/LibraryProcessing";
import type { ProcessForm } from "./processOptions";
import { RecallSelectionScope, useRecallSelection } from "./hooks/useRecallSelection";
import { WorkbenchPanel } from "./components/WorkbenchPanel";
import { ToolbarMenu } from "./components/ToolbarMenu";
import { VideoMoveMenu } from "./components/VideoMoveMenu";
import { StudyShareExportMenuItem, StudyShareImportButton } from "./components/StudyShareControls";
import { useTaskStatus, isTaskTerminal, taskLabel, videoFileChangeBlockReason } from "./hooks/useTaskStatus";
import type { Job, Video, VideoLibraryFolder, VideoLibraryState } from "./types";
import { canOpenReview, formatTime } from "./utils";
import "./styles.css";
import "./design-system.css";
import "./workbench.css";
import "./pages.css";
import "./review.css";
import "./theme.css";
import "./themes/character.css";
import "./library-glass.css";
import "./folder-navigation.css";
import "./onboarding.css";
import { CharacterAccent, CharacterEye } from "./components/CharacterAccent";
import { DesktopControls } from "./components/DesktopControls";
import { WarmAtmosphere } from "./components/WarmAtmosphere";
import { hasActivePreparation, preparationSummary } from "./components/ModelPreparationProgress";
import { cleanupSummary, isCleanupJob } from "./components/ModelCleanupProgress";
import { AnimatePresence, AnimatedOverlay, AnimatedPopup, motion, spotlight, useEntryMotion, useMotionTransition, useNewItems, useReducedMotion } from "./components/Motion";
import { OnboardingGuide } from "./components/OnboardingGuide";

const LIBRARY_SYNC_INTERVAL = 15_000;
const ProcessPage = lazy(() => import("./components/ProcessPage").then(module => ({ default: module.ProcessPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then(module => ({ default: module.SettingsPage })));
const BatchProcessPage = lazy(() => import("./pages/BatchProcessPage").then(module => ({ default: module.BatchProcessPage })));
const ReviewPage = lazy(() => import("./pages/ReviewPage").then(module => ({ default: module.ReviewPage })));
const TaskDetails = lazy(() => import("./components/TaskDetails").then(module => ({ default: module.TaskDetails })));

function PageLoading() {
  return <div className="library-state" role="status"><LoaderCircle className="spin" size={18} />正在打开页面…</div>;
}

function LocalError({ label, error }: { label: string; error: unknown }) {
  return <div className="local-error" role="status"><AlertCircle size={15} /><span>{label}加载失败：{errorMessage(error)}</span></div>;
}

export default function App() {
  // Keep observers and mutations on the same cache across React Fast Refresh.
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        retry: 0,
        refetchOnWindowFocus: false,
      },
    },
  }));

  const [router, setRouter] = useState<ReturnType<typeof createBrowserRouter> | null>(null);
  useEffect(() => {
    const next = createBrowserRouter([{ path: "*", element:
      <UnsavedChangesProvider><GlobalSearchProvider><ApplicationViews /><DesktopControls /></GlobalSearchProvider></UnsavedChangesProvider>,
    }]);
    setRouter(next);
    return () => next.dispose();
  }, []);
  return <QueryClientProvider client={queryClient}>{router && <RouterProvider router={router} />}</QueryClientProvider>;

}

function ApplicationViews() {
  const location = useLocation();
  const review = location.pathname === "/demo" || location.pathname.startsWith("/review/");
  return <ReviewTabsProvider>
    <Workbench visible={!review} />
    <Suspense fallback={<PageLoading />}><Routes>
      <Route path="/demo" element={<ReviewPage demo />} />
      <Route path="/review/:videoId" element={<ReviewPage />} />
      <Route path="*" element={null} />
    </Routes></Suspense>
  </ReviewTabsProvider>;
}

const PAGE_PATHS = ["/process", "/batch-process", "/settings"];

function Workbench({ visible }: { visible: boolean }) {
  const reviewTabs = useReviewTabs();
  const startup = useStartupLibraryView();
  const library = useQuery({
    queryKey: ["library-status"], queryFn: getLibraryStatus,
    enabled: visible, staleTime: 15_000, refetchInterval: visible ? 15_000 : false,
    refetchIntervalInBackground: false,
  });
  useEffect(() => {
    if (library.data?.library_id) reviewTabs?.setLibrary(library.data.library_id);
  }, [library.data?.library_id, reviewTabs?.setLibrary]);
  return <RecallSelectionScope.Provider value={library.data?.library_id || library.data?.library_scope || library.data?.data_root || "default"}>
    <WorkbenchContents key={library.data?.library_scope || library.data?.data_root || "default"} visible={visible}
      libraryId={library.data?.library_id} workspaceSettled={library.isFetched} startupView={startup.view} onStartupRestored={startup.dismiss}
      scope={library.data?.library_scope || library.data?.data_root || "default"}
      activeJobId={library.data?.active_job_id ?? null} />
  </RecallSelectionScope.Provider>;
}

function WorkbenchContents({ visible, scope, activeJobId, libraryId, workspaceSettled, startupView, onStartupRestored }: {
  visible: boolean; scope: string; activeJobId: string | number | null;
  libraryId?: string; workspaceSettled: boolean; startupView: StartupLibraryView | null; onStartupRestored: () => void;
}) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const background = useRef<HTMLDivElement>(null);
  const isPanel = visible && location.pathname === "/tasks";
  const pageLocation: Location = isPanel
    ? location.state?.backgroundLocation ?? { ...location, pathname: "/", search: "", state: null }
    : location;
  const isPage = PAGE_PATHS.includes(pageLocation.pathname);
  const libraryActive = visible && !isPage;
  const [screens, setScreens] = useState<Location[]>([]);
  const newDraftSequence = useRef(0);
  const [rememberedJob, setRememberedJob] = useState<string | number | null>(null);
  const [onboardingActive, setOnboardingActive] = useState(false);
  const panelKey = (value: Location) => value.pathname === "/process"
    ? value.state?.draftKey ?? `${value.pathname}:${new URLSearchParams(value.search).get("videoId") ?? "new"}` : value.pathname;
  useEffect(() => {
    if (!visible || !PAGE_PATHS.includes(location.pathname)) return;
    setScreens((current) => {
      const index = current.findIndex((screen) => panelKey(screen) === panelKey(location));
      if (index < 0) return [...current, location];
      if (current[index] === location) return current;
      return current.map((screen, position) => position === index ? location : screen);
    });
  }, [visible, location]);
  useEffect(() => {
    if (visible && !isPanel && !PAGE_PATHS.includes(location.pathname) && location.pathname !== "/" && !location.pathname.startsWith("/library/folder/")) {
      navigate("/", { replace: true });
    }
  }, [visible, isPanel, location.pathname, navigate]);
  const discoveryOptions = { enabled: visible, staleTime: 15_000, refetchInterval: visible ? 15_000 : false as const, refetchIntervalInBackground: false };
  const latest = useQuery({ queryKey: ["batch-latest"], queryFn: getLatestBatch, ...discoveryOptions });
  const paused = useQuery({ queryKey: ["paused-jobs"], queryFn: getPausedJobs, ...discoveryOptions });
  const fallback = paused.data?.[0] ?? latest.data;
  const remembered = queryClient.getQueryData<Job>(["jobs", rememberedJob]);
  const jobId = activeJobId && remembered && isTaskTerminal(remembered) ? activeJobId : rememberedJob ?? activeJobId ?? fallback?.id ?? null;
  const job = useTaskStatus(jobId, visible, libraryActive || isPanel || pageLocation.pathname === "/settings");
  const terminalSeen = useRef("");
  useEffect(() => {
    if (!job.data || !isTaskTerminal(job.data)) return;
    const key = `${job.data.id}:${job.data.status}`;
    if (key === terminalSeen.current) return;
    terminalSeen.current = key;
    void queryClient.invalidateQueries({ queryKey: ["videos"] });
    void queryClient.invalidateQueries({ queryKey: ["library"] });
    void queryClient.invalidateQueries({ queryKey: ["library-status"] });
  }, [job.data, queryClient]);
  const open = (path: string) => {
    const target = new URL(path, locationOrigin());
    if (`${target.pathname}${target.search}` === `${location.pathname}${location.search}`) return;
    const videoId = target.searchParams.get("videoId");
    const cached = target.pathname === "/process" ? screens.find((screen) =>
      screen.pathname === "/process" && new URLSearchParams(screen.search).get("videoId") === videoId) : undefined;
    const draftKey = target.pathname === "/process" ? cached ? panelKey(cached)
      : videoId ? `/process:${videoId}` : `/process:new:${newDraftSequence.current++}` : undefined;
    navigate(path, { state: {
      workbench: true, returnTo: `${location.pathname}${location.search}`,
      returnLocation: location, draftKey,
      backgroundLocation: target.pathname === "/tasks" ? pageLocation : undefined,
    } });
  };
  const close = () => {
    if (location.state?.workbench) navigate(-1);
    else navigate("/", { replace: true });
  };
  const onJob = (id: string | number) => {
    setRememberedJob(id);
    void queryClient.invalidateQueries({ queryKey: ["library-status"] });
    void queryClient.invalidateQueries({ queryKey: ["batch-latest"] });
  };
  const displayedScreens = isPage && !screens.some((screen) => panelKey(screen) === panelKey(pageLocation))
    ? [...screens, pageLocation] : screens;
  const currentTask = job.data ?? fallback ?? undefined;
  const taskRunning = Boolean(currentTask && ["running", "queued"].includes(currentTask.status));
  const TaskIcon = job.isError || currentTask?.status === "failed" ? AlertCircle
    : taskRunning ? LoaderCircle : currentTask?.status === "completed" ? CheckCircle2 : Clock3;
  return <div className="workbench" hidden={!visible} onClickCapture={(event) => {
    const anchor = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
    if (!anchor || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const url = new URL(anchor.href);
    if (url.origin === locationOrigin() && [...PAGE_PATHS, "/tasks"].includes(url.pathname)) {
      event.preventDefault();
      open(`${url.pathname}${url.search}`);
    }
  }}>
    <div ref={background}>
      <header className="workbench-topbar">
        <div className="workbench-topbar-inner">
          <Link className="workbench-brand" to="/"><Database size={21} /><CharacterEye /><strong>课堂重点复习</strong></Link>
          <div className="workbench-topbar-tools">
            <GlobalSearchButton />
            <Announcements active={visible} />
            <button type="button" className={`task-indicator ${currentTask ? `task-${currentTask.status}` : ""}`} onClick={() => open("/tasks")}
              title="查看任务详情" aria-label="查看任务详情" aria-haspopup="dialog" aria-expanded={isPanel}>
              <TaskIcon className={taskRunning && !job.isError ? "spin" : undefined} size={20} aria-hidden="true" />
              <span className="task-indicator-label">任务</span>
              <span className="task-indicator-status" role="status">{job.isError ? "状态读取失败" : currentTask?.stage === "download_paused" ? "下载已暂停"
                : currentTask?.stage === "pausing" ? "正在暂停下载" : currentTask && !isTaskTerminal(currentTask) && isCleanupJob(currentTask)
                  ? cleanupSummary(currentTask) : currentTask ? hasActivePreparation(currentTask)
                ? preparationSummary(currentTask)
                : `${taskLabel(currentTask.status)}${!isTaskTerminal(currentTask) ? ` · ${Math.round(currentTask.progress ?? 0)}%` : ""}` : "暂无任务"}</span>
              <ChevronRight size={15} aria-hidden="true" />
            </button>
            <Link data-onboarding="settings" className={`workbench-settings-link${pageLocation.pathname === "/settings" ? " is-selected" : ""}`}
              title="设置" aria-label="设置" aria-current={pageLocation.pathname === "/settings" ? "page" : undefined} to="/settings">
              <Settings2 size={20} aria-hidden="true" /><span>设置</span><UpdateNotice active={visible} />
            </Link>
          </div>
        </div>
        {visible && <ReviewTabs />}
      </header>
      <div hidden={!libraryActive}>
        <LibraryPage active={libraryActive} suspended={isPanel || !libraryActive} scope={scope}
          libraryId={libraryId} workspaceSettled={workspaceSettled} startupView={startupView} onStartupRestored={onStartupRestored}
          onJob={onJob} processingBlocked={Boolean(currentTask && (!isTaskTerminal(currentTask) || currentTask.status === "resource_paused"))}
          fileChangeJobs={[...(currentTask ? [currentTask] : []), ...(paused.data ?? [])]}
          routeFolderId={pageLocation.pathname.startsWith("/library/folder/") ? decodeURIComponent(pageLocation.pathname.split("/").at(-1)!) : null}
          onOpenPanel={open} />
      </div>
      {displayedScreens.map((stored) => {
        const shown = visible && isPage && panelKey(stored) === panelKey(pageLocation);
        const active = shown && !isPanel;
        const screen = shown ? pageLocation : stored;
        return <PageSession key={panelKey(stored)} active={shown} direction={navigationType === "POP" ? -1 : 1}>
          <div className="page-back-row"><button className="text-button" onClick={close}><ArrowLeft size={16} />{screen.state?.returnTo?.startsWith("/process") || screen.state?.returnTo?.startsWith("/batch-process") ? "返回处理" : "返回视频库"}</button></div>
          <Suspense fallback={<PageLoading />}><Routes location={screen}>
            <Route path="/process" element={<ProcessPage active={active} onClose={close} onJob={onJob}
              onRegistered={(video) => {
                if (active) navigate(`/process?videoId=${video.id}`, { replace: true, state: { ...location.state, draftKey: panelKey(screen) } });
              }} />} />
            <Route path="/batch-process" element={<BatchProcessPage active={active} onClose={close} onJob={onJob} />} />
            <Route path="/settings" element={<SettingsPage active={active} onClose={close} onJob={onJob}
              onConfigurationSaved={!onboardingActive && active && /^\/(process|batch-process|tasks)/.test(screen.state?.returnTo ?? "") ? close : undefined} />} />
          </Routes></Suspense>
        </PageSession>;
      })}
    </div>
    <WorkbenchPanel active={isPanel} scrollKey="tasks" title="任务详情" onClose={close} backgroundRef={background}>
      {isPanel && location.pathname === "/tasks" && <Suspense fallback={<PageLoading />}><TaskDetails job={job.data ?? fallback ?? undefined} loading={job.isLoading || latest.isLoading}
        error={job.error ?? latest.error ?? paused.error} onRetryLoad={() => { void job.refetch(); void latest.refetch(); void paused.refetch(); }}
        onJob={onJob} onSettings={() => open("/settings?section=ai")} onOpen={(value) => open(value.job_type === "storage_migration" ? "/settings?section=storage" : value.job_type === "batch_video_process" ? "/batch-process"
          : value.video_id ? `/process?videoId=${value.video_id}` : "/settings?section=asr")} /></Suspense>}
    </WorkbenchPanel>
    <OnboardingGuide active={visible && workspaceSettled && !isPanel} scope={scope} onActiveChange={setOnboardingActive} />
  </div>;
}

function PageSession({ active, direction, children }: { active: boolean; direction: number; children: ReactNode }) {
  const page = useRef<HTMLDivElement>(null);
  const scroll = useRef(0);
  useEntryMotion(page, { active });
  useEntryMotion(page, { active, axis: "x", distance: 6 * direction, fade: false,
    selector: ".page-back-row > button, .process-header, .process-section, .settings-page-heading, .settings-navigation" });
  useLayoutEffect(() => {
    if (!active) return;
    window.scrollTo({ top: scroll.current, behavior: "instant" });
    const save = () => { scroll.current = window.scrollY; };
    window.addEventListener("scroll", save, { passive: true });
    (page.current?.querySelector<HTMLElement>("h1") ?? page.current)?.focus({ preventScroll: true });
    return () => window.removeEventListener("scroll", save);
  }, [active]);
  return <div ref={page} hidden={!active} tabIndex={-1} className="workbench-screen">{children}</div>;
}

function locationOrigin() { return window.location.origin; }

interface LibraryPageProps {
  active: boolean; suspended: boolean; scope: string; routeFolderId: string | null;
  libraryId?: string; workspaceSettled: boolean; startupView: StartupLibraryView | null; onStartupRestored: () => void;
  onOpenPanel: (path: string) => void;
  onJob: (id: string | number) => void; processingBlocked: boolean;
  fileChangeJobs: Job[];
}

function LibraryPage({ active, suspended, scope, libraryId, workspaceSettled, startupView, onStartupRestored, routeFolderId, onOpenPanel, onJob, processingBlocked, fileChangeJobs }: LibraryPageProps) {
  const showDragTextHints = useDragTextHints();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const navigationKey = `classroom-library-navigation:${scope}`;
  const savedNavigation = useRef(readLibraryNavigation(navigationKey));
  const [folderId, setFolderId] = useState<string | null>(routeFolderId ?? savedNavigation.current.folderId);
  const [draggedKey, setDraggedKey] = useState<string | null>(null);
  const [selections, setSelections] = useState<Record<string, number[]>>({});
  const [folderSelections, setFolderSelections] = useState<Record<string, number[]>>({});
  const [processingOptions, setProcessingOptions] = useState<Record<string, boolean>>({});
  const [processingPending, setProcessingPending] = useState(false);
  const [processingFormOptions, setProcessingForm] = useState<ProcessForm>(DEFAULT_LIBRARY_PROCESS_FORM);
  const [recallModes] = useRecallSelection();
  const processingForm = { ...processingFormOptions, recallModes };
  const libraryBackgroundRef = useRef<HTMLDivElement>(null);
  const librarySurfaceRef = useRef<HTMLElement>(null);
  useEntryMotion(libraryBackgroundRef, { active, initial: false, duration: .14 });
  const folderTriggerRef = useRef<HTMLElement | null>(null);
  const folderScrolls = useRef<Record<string, number>>(savedNavigation.current.folderId
    ? { [savedNavigation.current.folderId]: savedNavigation.current.folderScroll } : {});
  const syncOptions = {
    staleTime: LIBRARY_SYNC_INTERVAL,
    enabled: active,
    refetchInterval: active && draggedKey === null ? LIBRARY_SYNC_INTERVAL : false as const,
    refetchIntervalInBackground: false,
    refetchOnMount: "always" as const,
    refetchOnWindowFocus: draggedKey === null ? "always" as const : false as const,
  };
  const videos = useQuery({
    queryKey: ["videos"],
    queryFn: getVideos,
    ...syncOptions,
  });
  const rootLibrary = useQuery({
    queryKey: ["library", "root"],
    queryFn: () => getLibrary(),
    ...syncOptions,
  });
  const library = useQuery({
    queryKey: ["library", folderId ?? "root"],
    queryFn: () => getLibrary(parseFolderId(folderId ?? null) ?? undefined),
    ...syncOptions,
    enabled: active && folderId !== null,
  });
  const videoList = useMemo(() => videos.data ?? [], [videos.data]);
  const storageKey = scope === "default" ? LIBRARY_STORAGE_KEY : `${LIBRARY_STORAGE_KEY}:${scope}`;
  const [libraryState, setLibraryState] = useState<VideoLibraryState>(() => readLibraryState(storageKey));
  const [sortMode, setSortMode] = useState<"manual" | "name" | "time">(savedNavigation.current.sortMode);
  const [folderSortMode, setFolderSortMode] = useState<"manual" | "name" | "time">(savedNavigation.current.folderSortMode);
  const [newFolderName, setNewFolderName] = useState("");
  const [isCreatingFolder, setIsCreatingFolder] = useState(false);
  const dragSourceRef = useRef<{ key: string; folderId: string | null } | null>(null);
  const [dropIntent, setDropIntent] = useState<DropIntent | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [renamingFolder, setRenamingFolder] = useState<VideoLibraryFolder | null>(null);
  const [renameName, setRenameName] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [renamingVideo, setRenamingVideo] = useState<Video | null>(null);
  const [videoFilename, setVideoFilename] = useState("");
  const [videoRenameBusy, setVideoRenameBusy] = useState(false);
  const [videoRenameError, setVideoRenameError] = useState<string | null>(null);
  const videoRenameSaving = useRef(false);
  const videoFilenameRef = useRef<HTMLInputElement>(null);
  const videoRenameTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const creatingFolder = useRef(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<{ text: string; retry: () => void } | null>(null);
  const [trashBusyKey, setTrashBusyKey] = useState<string | null>(null);
  const [recycleTarget, setRecycleTarget] = useState<
    { kind: "video"; video: Video } | { kind: "folder"; folder: VideoLibraryFolder } | null
  >(null);
  const [canUndoRemoval, setCanUndoRemoval] = useState(false);
  const [undoingRemoval, setUndoingRemoval] = useState(false);
  const recycleLatest = useQuery({
    queryKey: ["recycle-bin-latest"], queryFn: getLatestRecycleBin, enabled: active,
    staleTime: 15_000, refetchOnWindowFocus: true,
  });
  const movingIds = useRef(new Set<number>());
  const [moving, setMoving] = useState<ReadonlySet<number>>(new Set());
  const movingFolderIds = useRef(new Set<string>());
  const [movingFolders, setMovingFolders] = useState<ReadonlySet<string>>(new Set());
  const suppressFolderClickUntil = useRef(0);
  const suppressVideoClickUntil = useRef(0);
  const wasActive = useRef(active);
  const restoredRoot = useRef(false);
  const startupAnchor = useRef<StartupLibraryView | null>(null);
  const [reconciledInputs, setReconciledInputs] = useState<{
    root: typeof rootLibrary.data; videos: typeof videos.data;
  } | null>(null);
  const folderDialogRef = useRef<HTMLFormElement>(null);
  const folderDialogCloseRef = useRef<HTMLButtonElement>(null);
  useModalFocus(active && !suspended && Boolean(renamingFolder || isCreatingFolder), folderDialogRef, folderDialogCloseRef,
    () => { if (!renameBusy && !createBusy) { setRenamingFolder(null); setIsCreatingFolder(false); } }, libraryBackgroundRef);

  useLayoutEffect(() => {
    if (renamingVideo) {
      const input = videoFilenameRef.current;
      input?.focus({ preventScroll: true });
      const extension = renamingVideo.filename.lastIndexOf(".");
      input?.setSelectionRange(0, extension > 0 ? extension : renamingVideo.filename.length);
    } else if (videoRenameTriggerRef.current) {
      const trigger = videoRenameTriggerRef.current;
      if (trigger.isConnected && !trigger.closest("[hidden], [inert]")) trigger.focus({ preventScroll: true });
      videoRenameTriggerRef.current = null;
    }
  }, [renamingVideo]);

  useEffect(() => { if (routeFolderId) setFolderId(routeFolderId); }, [routeFolderId]);
  useEffect(() => {
    savedNavigation.current = { ...savedNavigation.current, folderId, sortMode, folderSortMode,
      folderScroll: folderId ? folderScrolls.current[folderId] ?? 0 : 0 };
    saveLibraryNavigation(navigationKey, savedNavigation.current);
  }, [folderId, sortMode, folderSortMode, navigationKey]);
  useEffect(() => {
    if (active && !wasActive.current) {
      void videos.refetch(); void rootLibrary.refetch();
      restoredRoot.current = false;
    }
    wasActive.current = active;
  }, [active]);
  useEffect(() => {
    if (!active || suspended) return;
    const saveScroll = () => {
      savedNavigation.current.rootScroll = window.scrollY;
      saveLibraryNavigation(navigationKey, savedNavigation.current);
    };
    window.addEventListener("scroll", saveScroll, { passive: true });
    return () => window.removeEventListener("scroll", saveScroll);
  }, [active, suspended, navigationKey]);
  useEffect(() => {
    if (!active || !rootLibrary.data || restoredRoot.current) return;
    restoredRoot.current = true;
    window.scrollTo({ top: savedNavigation.current.rootScroll, behavior: "instant" });
  }, [active, rootLibrary.data]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (draggedKey !== null) return;
    setLibraryState((current) => reconcileLibraryState(
      current,
      videoList,
      library.data?.items,
      parseFolderId(folderId ?? null),
      rootLibrary.data?.items,
      library.data?.ancestors ?? (library.data?.folder ? [library.data.folder] : []),
    ));
    setReconciledInputs({ root: rootLibrary.data, videos: videos.data });
  }, [videoList, library.data, folderId, rootLibrary.data, draggedKey]);

  useEffect(() => {
    try { window.localStorage.setItem(storageKey, JSON.stringify(libraryState)); } catch { /* The server remains authoritative. */ }
  }, [libraryState, storageKey]);

  const activeFolder = folderId ? libraryState.folders.find((folder) => folder.id === folderId) : undefined;
  const rootVideos = useMemo(
    () => sortVideos(videoList.filter((video) => !libraryState.assignments[String(video.id)]), sortMode, libraryState.rootOrder),
    [videoList, libraryState.assignments, sortMode, libraryState.rootOrder],
  );
  const scopedVideos = videoList.filter((video) => (libraryState.assignments[String(video.id)] ?? null) === (folderId ?? null));
  const folderVideos = useMemo(
    () => sortVideos(scopedVideos, folderSortMode, folderId ? libraryState.folderOrders[folderId] ?? [] : []),
    [scopedVideos, folderSortMode, folderId, libraryState.folderOrders],
  );
  const visibleFolders = useMemo(
    () => sortFolders(libraryState.folders.filter((folder) => !folder.parentId), sortMode, libraryState.rootOrder),
    [libraryState.folders, sortMode, libraryState.rootOrder],
  );
  const rootItems = useMemo(
    () => sortLibraryItems(
      [
        ...libraryState.folders.filter((folder) => !folder.parentId).map((folder) => ({ kind: "folder" as const, key: folderKey(folder), folder })),
        ...rootVideos.map((video) => ({ kind: "video" as const, key: videoKey(video), video })),
      ],
      sortMode,
      libraryState.rootOrder,
    ),
    [libraryState.folders, rootVideos, sortMode, libraryState.rootOrder],
  );
  useLayoutEffect(() => {
    if (!startupView || !workspaceSettled || routeFolderId || !active || suspended
      || !rootLibrary.data || !videos.data || reconciledInputs?.root !== rootLibrary.data
      || reconciledInputs.videos !== videos.data) return;
    if (!libraryId) { onStartupRestored(); return; }
    if (startupView.library_id === libraryId) {
      if (folderId) { setFolderId(null); return; }
      const anchor = startupView.anchor
        ? libraryBackgroundRef.current?.querySelector<HTMLElement>(`[data-library-key="${startupView.anchor}"]`) : null;
      // Folder reconciliation can commit after the queries and native bridge have settled.
      if (!anchor && startupView.anchor && rootLibrary.data.items.some(item =>
        `${item.type}-${item.folder?.id ?? item.video?.id}` === startupView.anchor)) return;
      const top = anchor ? window.scrollY + anchor.getBoundingClientRect().top - startupView.offset : startupView.scroll_y;
      window.scrollTo({ top: Math.max(0, top), behavior: "instant" });
      startupAnchor.current = startupView;
      savedNavigation.current.rootScroll = window.scrollY;
      saveLibraryNavigation(navigationKey, savedNavigation.current);
      restoredRoot.current = true;
    }
    onStartupRestored();
  }, [active, suspended, rootLibrary.data, videos.data, reconciledInputs, rootItems, startupView, libraryId, workspaceSettled, routeFolderId,
    folderId, onStartupRestored, navigationKey]);
  useEffect(() => {
    if (!document.getElementById("classroom-startup-cover")) return;
    if (!active || suspended) return releaseStartupCover(null);
    if (workspaceSettled && (videos.isError || rootLibrary.isError)) {
      if (startupView) onStartupRestored();
      return releaseStartupCover(libraryBackgroundRef.current);
    }
    if (!workspaceSettled || startupView || videos.isLoading || rootLibrary.isLoading) return;
    if (reconciledInputs?.root !== rootLibrary.data || reconciledInputs?.videos !== videos.data) return;
    return releaseStartupCover(libraryBackgroundRef.current, () => {
      const view = startupAnchor.current;
      startupAnchor.current = null;
      const anchor = view?.anchor
        ? libraryBackgroundRef.current?.querySelector<HTMLElement>(`[data-library-key="${view.anchor}"]`) : null;
      if (!view || !anchor) return;
      // Fonts and the final grid may have settled after the first layout commit.
      const top = window.scrollY + anchor.getBoundingClientRect().top - view.offset;
      window.scrollTo({ top: Math.max(0, top), behavior: "instant" });
      savedNavigation.current.rootScroll = window.scrollY;
      saveLibraryNavigation(navigationKey, savedNavigation.current);
    });
  }, [active, suspended, workspaceSettled, startupView, videos.isLoading, rootLibrary.isLoading,
    videos.isError, rootLibrary.isError, rootLibrary.data, videos.data, reconciledInputs, rootItems, onStartupRestored, navigationKey]);
  const libraryMotion = useMotionTransition(.22);
  const reducedMotion = useReducedMotion();
  const rootNewItems = useNewItems(rootItems.map((item) => item.key), !videos.isLoading && !rootLibrary.isLoading);
  const folderItems = sortLibraryItems([
    ...libraryState.folders.filter((folder) => folder.parentId === folderId)
      .map((folder) => ({ kind: "folder" as const, key: folderKey(folder), folder })),
    ...folderVideos.map((video) => ({ kind: "video" as const, key: videoKey(video), video })),
  ], folderSortMode, folderId ? libraryState.folderOrders[folderId] ?? [] : []);
  const folderNewItems = useNewItems(folderItems.map((item) => item.key), Boolean(folderId) && !library.isLoading && !videos.isLoading, folderId ?? "root");
  const folderBreadcrumbs: VideoLibraryFolder[] = [];
  let breadcrumbFolder = activeFolder;
  const visitedFolders = new Set<string>();
  while (breadcrumbFolder && !visitedFolders.has(breadcrumbFolder.id)) {
    visitedFolders.add(breadcrumbFolder.id);
    folderBreadcrumbs.unshift(breadcrumbFolder);
    breadcrumbFolder = libraryState.folders.find((folder) => folder.id === breadcrumbFolder?.parentId);
  }
  useEffect(() => {
    const eligible = new Set(videoList.filter(canSelectForProcessing).map((video) => video.id));
    setSelections((current) => {
      let changed = false;
      const next = Object.fromEntries(Object.entries(current).map(([key, ids]) => {
        const remaining = ids.filter((id) => eligible.has(id)
          && (libraryState.assignments[String(id)] ?? "root") === key);
        if (remaining.length !== ids.length) changed = true;
        return [key, remaining];
      }));
      return changed ? next : current;
    });
  }, [videoList, libraryState.assignments]);

  useEffect(() => {
    setFolderSelections(current => {
      let changed = false;
      const next = Object.fromEntries(Object.entries(current).map(([key, ids]) => {
        const remaining = ids.filter(id => libraryState.folders.some(folder => parseFolderId(folder.id) === id
          && (folder.parentId ?? "root") === key && countFolderVideos(folder.id, videoList, libraryState) > 0));
        if (remaining.length !== ids.length) changed = true;
        return [key, remaining];
      }));
      return changed ? next : current;
    });
  }, [libraryState, videoList]);

  const processingControls = (items: Video[], parentId: string | null = null) => {
    const key = parentId ?? "root";
    return <LibraryProcessing videos={items} selectedIds={selections[key] ?? []}
      folders={libraryState.folders.filter(folder => (folder.parentId ?? null) === parentId).flatMap(folder => {
        const id = parseFolderId(folder.id);
        return id === null ? [] : [{ id, name: folder.name, videoCount: countFolderVideos(folder.id, videoList, libraryState) }];
      })}
      selectedFolderIds={folderSelections[key] ?? []}
      onSelectFolders={ids => setFolderSelections(current => ({ ...current, [key]: ids }))}
      form={processingForm} onFormChange={setProcessingForm}
      onSelect={(ids) => setSelections((current) => ({ ...current, [key]: ids }))}
      expanded={Boolean(processingOptions[key])} onExpand={(open) => {
        if (open) setNotice(null);
        setProcessingOptions((current) => ({ ...current, [key]: open }));
      }}
      active={active && !suspended && (parentId ? folderId === parentId : !folderId)}
      blocked={processingBlocked} pending={processingPending} onPending={setProcessingPending}
      onSubmitted={(id, count) => {
        onJob(id);
        setSelections((current) => ({ ...current, [key]: [] }));
        setFolderSelections(current => ({ ...current, [key]: [] }));
        setProcessingOptions((current) => ({ ...current, [key]: false }));
        setNotice(count > 1 ? `已提交批处理，共 ${count} 个视频` : "已提交视频处理任务");
      }} />;
  };

  const updateOrder = (sourceKey: string, targetKey: string, placement: "before" | "after" | "swap" = "swap", parentId: string | null = null) => {
    if (!sourceKey || sourceKey === targetKey) return;
    if (sourceKey.startsWith("video-") !== targetKey.startsWith("video-")) return;
    const current = parentId ? libraryState.folderOrders[parentId] ?? [] : libraryState.rootOrder;
    const keys = (parentId ? folderItems : rootItems).map((item) => item.key);
    const allOrdered = mergeOrder(keys, current);
    const ordered = allOrdered.filter((key) => key.startsWith("video-") === sourceKey.startsWith("video-"));
    const from = ordered.indexOf(sourceKey);
    const to = ordered.indexOf(targetKey);
    if (from < 0 || to < 0) return;
    const reordered = [...ordered];
    if (placement === "swap") {
      [reordered[from], reordered[to]] = [reordered[to], reordered[from]];
    } else {
      reordered.splice(from, 1);
      const targetIndex = reordered.indexOf(targetKey);
      reordered.splice(placement === "after" ? targetIndex + 1 : targetIndex, 0, sourceKey);
    }
    let index = 0;
    const next = allOrdered.map((key) => key.startsWith("video-") === sourceKey.startsWith("video-") ? reordered[index++] : key);
    setLibraryState((state) => parentId
      ? { ...state, folderOrders: { ...state.folderOrders, [parentId]: next } }
      : { ...state, rootOrder: next });
    if (parentId) setFolderSortMode("manual");
    else setSortMode("manual");
    const orderedItems: Array<{ type: "folder" | "video"; id: number }> = [];
    for (const key of next) {
      if (key.startsWith("video-")) {
        const id = Number(key.slice("video-".length));
        if (Number.isFinite(id)) orderedItems.push({ type: "video", id });
        continue;
      }
      const id = parseFolderId(key);
      if (id !== null) orderedItems.push({ type: "folder", id });
    }
    const saveOrder = () => {
      setOperationError(null);
      void reorderLibrary(parseFolderId(parentId), orderedItems)
        .then(() => void queryClient.invalidateQueries({ queryKey: ["library"] }))
        .catch((error) => setOperationError({ text: `排序保存失败：${errorMessage(error)}`, retry: saveOrder }));
    };
    saveOrder();
  };

  const canMoveFolder = (sourceId: string, destination: string | null) => {
    const source = libraryState.folders.find((folder) => folder.id === sourceId);
    if (!source || movingFolderIds.current.has(sourceId) || (source.parentId ?? null) === destination) return false;
    const visited = new Set([sourceId]);
    let ancestor = destination;
    while (ancestor) {
      if (visited.has(ancestor)) return false;
      visited.add(ancestor);
      const folder = libraryState.folders.find((candidate) => candidate.id === ancestor);
      if (!folder) return false;
      ancestor = folder.parentId ?? null;
    }
    return true;
  };

  const resolveDropIntent = (event: DragEvent<HTMLElement>, item: LibraryItem, parentId: string | null = null): DropIntent | null => {
    if (!draggedKey || draggedKey === item.key) return null;
    if (item.kind === "folder" && draggedKey.startsWith("video-")) {
      return { targetKey: item.key, mode: "folder", edge: null };
    }
    if ((item.kind === "video") !== draggedKey.startsWith("video-")) return null;
    const rect = event.currentTarget.getBoundingClientRect();
    const vertical = false;
    const axisSize = vertical ? rect.height : rect.width;
    const axisPosition = vertical ? event.clientY - rect.top : event.clientX - rect.left;
    const edgeBand = Math.min(64, Math.max(32, axisSize * 0.2));
    const previous = dropIntent?.targetKey === item.key ? dropIntent : null;
    const hysteresis = 8;
    const enterBand = Math.max(24, edgeBand - hysteresis);
    const leaveBand = edgeBand + hysteresis;
    const band = previous?.mode === "swap" ? leaveBand : enterBand;
    const edge = axisPosition <= band
      ? "before"
      : axisPosition >= axisSize - band
        ? "after"
        : null;
    if (item.kind === "folder" && edge === null) {
      return canMoveFolder(draggedKey, item.folder.id)
        ? { targetKey: item.key, mode: "folder", edge: null } : null;
    }
    if (dragSourceRef.current?.folderId !== parentId) return null;
    return {
      targetKey: item.key,
      mode: "swap",
      edge,
    };
  };

  const handleDragOver = (event: DragEvent<HTMLElement>, item: LibraryItem, parentId: string | null = null) => {
    event.preventDefault();
    const intent = resolveDropIntent(event, item, parentId);
    setDropIntent(intent);
    event.dataTransfer.dropEffect = intent ? "move" : "none";
  };

  const clearDragState = () => {
    if (suppressFolderClickUntil.current === Number.POSITIVE_INFINITY) suppressFolderClickUntil.current = Date.now() + 250;
    if (suppressVideoClickUntil.current === Number.POSITIVE_INFINITY) suppressVideoClickUntil.current = Date.now() + 250;
    dragSourceRef.current = null;
    setDraggedKey(null);
    setDropIntent(null);
  };

  const handleDrop = (event: DragEvent<HTMLElement>, item: LibraryItem, parentId: string | null = null) => {
    event.preventDefault();
    event.stopPropagation();
    const intent = resolveDropIntent(event, item, parentId);
    if (!draggedKey || !intent || intent.targetKey !== item.key) {
      clearDragState();
      return;
    }
    if (intent.mode === "folder" && item.kind === "folder") {
      if (draggedKey.startsWith("video-")) {
        const videoId = Number(draggedKey.slice("video-".length));
        if (Number.isFinite(videoId)) void moveVideo(videoId, item.folder.id);
      } else {
        void moveFolder(draggedKey, item.folder.id);
      }
    } else {
      updateOrder(draggedKey, item.key, intent.edge ?? "swap", parentId);
    }
    clearDragState();
  };

  const handleDragLeave = (event: DragEvent<HTMLElement>) => {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    const targetKey = event.currentTarget.getAttribute("data-library-key");
    setDropIntent((current) => current?.targetKey === targetKey ? null : current);
  };

  const createFolder = () => {
    const name = newFolderName.trim();
    if (!name || creatingFolder.current) return;
    creatingFolder.current = true;
    setCreateBusy(true);
    setCreateError(null);
    void createLibraryFolder(name, parseFolderId(folderId)).then((created) => {
      const folder: VideoLibraryFolder = { id: `folder-${created.id}`, name: created.name,
        parentId: created.parent_id == null ? null : `folder-${created.parent_id}`,
        createdAt: Date.parse(created.created_at ?? "") || Date.now() };
      setLibraryState((state) => ({ ...state, folders: [...state.folders, folder],
        rootOrder: folder.parentId ? state.rootOrder : [...state.rootOrder, folderKey(folder)],
        folderOrders: folder.parentId ? { ...state.folderOrders,
          [folder.parentId]: [...(state.folderOrders[folder.parentId] ?? []), folderKey(folder)] } : state.folderOrders }));
      void queryClient.invalidateQueries({ queryKey: ["library"] });
      setNewFolderName("");
      setIsCreatingFolder(false);
      setNotice(`已创建文件夹“${name}”`);
    }).catch((error) => setCreateError(`文件夹创建失败：${errorMessage(error)}`))
      .finally(() => { creatingFolder.current = false; setCreateBusy(false); });
  };

  const moveVideo = async (videoId: number, destination: string) => {
    if (movingIds.current.has(videoId)) return;
    const nextFolder = destination || null;
    const nextFolderApiId = parseFolderId(nextFolder);
    if (nextFolder && nextFolderApiId === null) {
      setOperationError({ text: "视频移动失败：文件夹不存在", retry: () => { void rootLibrary.refetch(); } });
      return;
    }
    movingIds.current.add(videoId);
    setMoving(new Set(movingIds.current));
    setOperationError(null);
    try {
      const savedVideo = await moveVideoToFolder(videoId, nextFolderApiId);
      queryClient.setQueryData<Video[]>(["videos"], (current) => current?.map((video) => video.id === videoId ? savedVideo : video));
      const savedFolder = savedVideo.folder_id == null ? null : `folder-${savedVideo.folder_id}`;
      setLibraryState((state) => ({
        ...state,
        assignments: { ...state.assignments, [String(videoId)]: savedFolder },
      }));
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      void queryClient.invalidateQueries({ queryKey: ["library"] });
      setNotice(savedFolder ? "视频已移入文件夹" : "视频已放回一级目录");
    } catch (error) {
      setOperationError({ text: `视频移动失败：${errorMessage(error)}`, retry: () => { void moveVideo(videoId, destination); } });
    } finally {
      movingIds.current.delete(videoId);
      setMoving(new Set(movingIds.current));
    }
  };

  const moveFolder = async (sourceId: string, destination: string | null) => {
    const apiId = parseFolderId(sourceId);
    if (apiId === null || !canMoveFolder(sourceId, destination)) return;
    movingFolderIds.current.add(sourceId);
    setMovingFolders(new Set(movingFolderIds.current));
    setOperationError(null);
    try {
      await moveLibraryFolder(apiId, parseFolderId(destination));
      await queryClient.invalidateQueries({ queryKey: ["library"] });
      setNotice(destination ? "文件夹已移入目标文件夹" : "文件夹已放回一级目录");
    } catch (error) {
      setOperationError({ text: `文件夹移动失败：${errorMessage(error)}`, retry: () => { void moveFolder(sourceId, destination); } });
    } finally {
      movingFolderIds.current.delete(sourceId);
      setMovingFolders(new Set(movingFolderIds.current));
    }
  };

  const saveFolderName = async () => {
    const name = renameName.trim();
    if (!renamingFolder || !name || renameBusy) return;
    setRenameBusy(true);
    setRenameError(null);
    try {
      const apiId = parseFolderId(renamingFolder.id);
      if (apiId !== null) await renameLibraryFolder(apiId, name);
      setLibraryState((state) => ({
        ...state,
        folders: state.folders.map((folder) => folder.id === renamingFolder.id ? { ...folder, name } : folder),
      }));
      if (apiId !== null) await queryClient.invalidateQueries({ queryKey: ["library"] });
      setRenamingFolder(null);
      setNotice(`文件夹已重命名为“${name}”`);
    } catch (error) {
      setRenameError(errorMessage(error));
    } finally {
      setRenameBusy(false);
    }
  };

  const saveVideoName = async () => {
    const filename = videoFilename.trim();
    if (!renamingVideo || !filename || filename === renamingVideo.filename
      || videoRenameSaving.current) return;
    const currentVideo = videoList.find(video => video.id === renamingVideo.id) ?? renamingVideo;
    const blocked = videoFileChangeBlockReason(currentVideo, fileChangeJobs);
    if (blocked) { setVideoRenameError(blocked); return; }
    videoRenameSaving.current = true;
    setVideoRenameBusy(true);
    setVideoRenameError(null);
    try {
      const savedVideo = await renameVideoFile(renamingVideo.id, filename);
      queryClient.setQueryData<Video[]>(["videos"], (current) =>
        current?.map((video) => video.id === savedVideo.id ? savedVideo : video));
      for (const queryKey of [
        ["videos"], ["library"], ["process", "video", savedVideo.id], ["study", "video", savedVideo.id],
      ]) void queryClient.invalidateQueries({ queryKey });
      setRenamingVideo(null);
      setNotice(savedVideo.storage_warning || `视频已重命名为“${savedVideo.filename}”`);
    } catch (error) {
      setVideoRenameError(errorMessage(error));
    } finally {
      videoRenameSaving.current = false;
      setVideoRenameBusy(false);
    }
  };

  const undoRemoval = async () => {
    if (!canUndoRemoval || undoingRemoval || recycleTarget) return;
    setUndoingRemoval(true);
    setOperationError(null);
    try {
      const restored = await undoRecycleBin();
      setCanUndoRemoval(false);
      setNotice(restored.videos > 1 ? `已恢复 ${restored.videos} 个视频` : "已恢复到视频库");
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      void queryClient.invalidateQueries({ queryKey: ["library"] });
      void queryClient.invalidateQueries({ queryKey: ["library-status"] });
      void queryClient.invalidateQueries({ queryKey: ["recycle-bin-latest"] });
    } catch (error) {
      setOperationError({ text: `撤销删除失败：${errorMessage(error)}`, retry: () => { void undoRemoval(); } });
    } finally {
      setUndoingRemoval(false);
    }
  };

  useEffect(() => {
    if (!active || suspended) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z"
        || event.defaultPrevented || event.isComposing) return;
      const target = event.target;
      if (target instanceof HTMLElement
        && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))) return;
      if (!canUndoRemoval || undoingRemoval || recycleTarget) return;
      event.preventDefault();
      void undoRemoval();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [active, suspended, canUndoRemoval, undoingRemoval, recycleTarget]);

  useEffect(() => {
    if (recycleLatest.isSuccess) setCanUndoRemoval(Boolean(recycleLatest.data));
  }, [recycleLatest.data, recycleLatest.isSuccess]);

  const confirmRemoval = async () => {
    if (!recycleTarget) return;
    const target = recycleTarget;
    setTrashBusyKey(target.kind === "folder" ? `folder-${target.folder.id}` : `video-${target.video.id}`);
    setOperationError(null);
    try {
      const result = target.kind === "folder"
        ? await deleteLibraryFolder(parseFolderId(target.folder.id)!)
        : await deleteVideo(target.video.id);
      setCanUndoRemoval(true);
      setNotice(target.kind === "folder"
        ? result.videos ? `文件夹已移入回收站（${result.videos} 个视频），按 Ctrl+Z 撤销` : "文件夹已移入回收站，按 Ctrl+Z 撤销"
        : result.message || "视频已移入回收站，按 Ctrl+Z 撤销");
      if (target.kind === "folder" && folderId === target.folder.id) setFolderId(null);
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      void queryClient.invalidateQueries({ queryKey: ["library"] });
      void queryClient.invalidateQueries({ queryKey: ["library-status"] });
      void queryClient.invalidateQueries({ queryKey: ["recycle-bin-latest"] });
    } finally {
      setTrashBusyKey(null);
    }
  };

  const closeFolder = () => {
    clearDragState();
    setFolderId(null);
    if (routeFolderId) navigate("/", { replace: true });
  };

  const moveDraggedItemTo = (destination: string | null) => {
    const source = dragSourceRef.current;
    if (!folderId || source?.folderId !== folderId) return;
    clearDragState();
    if (source.key.startsWith("video-")) {
      const videoId = Number(source.key.slice("video-".length));
      if (Number.isFinite(videoId)) void moveVideo(videoId, destination ?? "");
    } else {
      void moveFolder(source.key, destination);
    }
  };

  const dragFeedback = (item: LibraryItem) => {
    if (dropIntent?.targetKey !== item.key) return null;
    const intoFolder = dropIntent.mode === "folder" && item.kind === "folder";
    const Icon = intoFolder ? FolderInput : dropIntent.edge === "before" ? ArrowLeftToLine
      : dropIntent.edge === "after" ? ArrowRightToLine : ArrowLeftRight;
    const text = intoFolder ? `移入「${item.folder.name}」`
      : dropIntent.edge === "before" ? "放在前面" : dropIntent.edge === "after" ? "放在后面" : "交换位置";
    return <>
      {dropIntent.edge && <span className="library-drop-line" data-edge={dropIntent.edge} aria-hidden="true" />}
      {showDragTextHints && <span className={`library-drop-feedback ${intoFolder ? "is-move" : "is-order"}`} role="status">
      <Icon size={15} /><span>{text}</span>
      </span>}
    </>;
  };

  const startupTransitionActive = Boolean(startupView || document.getElementById("classroom-startup-cover"));
  const renderLibraryItem = (item: LibraryItem, parentId: string | null = null) => {
    const entryMotion = {
      initial: !startupTransitionActive && (parentId ? folderNewItems : rootNewItems).has(item.key) && !reducedMotion ? { opacity: 0, y: 6 } : false as const,
      animate: { opacity: draggedKey === item.key ? .5 : 1, y: 0 }, transition: libraryMotion,
      layout: !startupTransitionActive && draggedKey === null && !reducedMotion ? "position" as const : false as const,
      onPointerMove: spotlight, onPointerLeave: (event: React.PointerEvent<HTMLElement>) => { delete event.currentTarget.dataset.spotlight; },
    };
    if (item.kind === "folder") {
      const id = parseFolderId(item.folder.id);
      const selectionKey = parentId ?? "root";
      const selected = id !== null && (folderSelections[selectionKey] ?? []).includes(id);
      const count = countFolderVideos(item.folder.id, videoList, libraryState);
      const selectionDisabled = processingBlocked || processingPending || movingFolders.has(item.key) || id === null || count === 0;
      return (
        <motion.article {...entryMotion}
          className={`library-card folder-card${selected ? " is-selected" : ""}${movingFolders.has(item.key) ? " is-busy" : ""}${dropIntent?.targetKey === item.key ? ` drop-${dropIntent.mode}${dropIntent.edge ? ` drop-${dropIntent.edge}` : ""}` : ""}`}
          key={item.key}
          draggable={!movingFolders.has(item.key)}
          aria-busy={movingFolders.has(item.key)}
          data-library-key={item.key}
          data-drag-source={draggedKey === item.key || undefined}
          onDragStartCapture={(event) => {
            event.dataTransfer.effectAllowed = "move";
            suppressFolderClickUntil.current = Number.POSITIVE_INFINITY;
            dragSourceRef.current = { key: item.key, folderId: parentId };
            setDraggedKey(item.key);
          }}
          onDragOver={(event) => handleDragOver(event, item, parentId)}
          onDragLeave={handleDragLeave}
          onDrop={(event) => handleDrop(event, item, parentId)}
          onDragEnd={() => {
            suppressFolderClickUntil.current = Date.now() + 250;
            clearDragState();
          }}
          onClickCapture={(event) => {
            if (Date.now() < suppressFolderClickUntil.current) {
              event.preventDefault();
              event.stopPropagation();
            }
          }}
        >
          <label className="library-folder-select" title={count === 0 ? "此文件夹没有视频" : `选择此文件夹的全部 ${count} 个视频（含子文件夹）`}
            onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()}
            onDragStart={event => { event.preventDefault(); event.stopPropagation(); }}>
            <input type="checkbox" aria-label={`选择文件夹“${item.folder.name}”中的所有视频`}
              checked={selected} disabled={selectionDisabled} onChange={event => {
                if (id === null) return;
                const checked = event.target.checked;
                setFolderSelections(current => ({ ...current, [selectionKey]: checked
                  ? [...new Set([...(current[selectionKey] ?? []), id])]
                  : (current[selectionKey] ?? []).filter(value => value !== id) }));
              }} />
          </label>
          <button className="folder-card-link" type="button" aria-label={`打开文件夹“${item.folder.name}”`} draggable={false}
            onClick={(event) => {
              if (!folderId) folderTriggerRef.current = event.currentTarget;
              clearDragState();
              setFolderId(item.folder.id);
            }}>
            <span className="library-card-icon folder-icon">
              {dropIntent?.targetKey === item.key && dropIntent.mode === "folder" ? <FolderInput size={22} /> : <Folder size={22} />}
            </span>
            <span className="library-card-body">
              <strong title={item.folder.name}>{item.folder.name}</strong>
              <span title="包含所有子文件夹中的视频">{countFolderVideos(item.folder.id, videoList, libraryState)} 个视频</span>
              <MarkerImportanceCounts counts={item.folder.markerImportanceCounts} folder />
            </span>
          </button>
          <button className="icon-button folder-rename-button" type="button" title="重命名文件夹" aria-label={`重命名文件夹“${item.folder.name}”`}
            onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setRenamingFolder(item.folder);
            setRenameName(item.folder.name);
            setRenameError(null);
          }}><Pencil size={16} /></button>
          <button className="icon-button danger-icon folder-delete-button" type="button" title="移入回收站" aria-label={`删除文件夹“${item.folder.name}”`}
            disabled={trashBusyKey !== null} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setRecycleTarget({ kind: "folder", folder: item.folder });
            }}>
            {trashBusyKey === `folder-${item.folder.id}` ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}
          </button>
          <span className="drag-handle" title={movingFolders.has(item.key) ? "正在移动文件夹" : "拖到文件夹中央移入，拖到边缘排序"}>
            {movingFolders.has(item.key) ? <LoaderCircle className="spin" size={17} /> : <GripVertical size={17} />}
          </span>
          {dragFeedback(item)}
        </motion.article>
      );
    }
    const video = item.video;
    const editingVideoName = renamingVideo?.id === video.id;
    const renameBlockReason = videoFileChangeBlockReason(video, fileChangeJobs);
    const selectionKey = parentId ?? "root";
    const selected = (selections[selectionKey] ?? []).includes(video.id);
    const processingDisabled = processingBlocked || processingPending || moving.has(video.id) || !canSelectForProcessing(video);
    const ready = video.status === "review_ready";
    const preview = canOpenReview(video.status, Boolean(video.has_transcript));
    const openVideo = () => {
      if (Date.now() < suppressVideoClickUntil.current || moving.has(video.id)) return;
      if (ready || preview) navigate(`/review/${video.id}`);
      else onOpenPanel(`/process?videoId=${video.id}`);
    };
    return (
      <motion.article {...entryMotion}
        className={`library-card video-card${selected ? " is-selected" : ""}${moving.has(video.id) ? " is-busy" : ""}${dropIntent?.targetKey === item.key ? ` drop-swap${dropIntent.edge ? ` drop-${dropIntent.edge}` : ""}` : ""}`}
        key={item.key}
        draggable={!editingVideoName}
        data-library-key={item.key}
        data-drag-source={draggedKey === item.key || undefined}
        onDragStartCapture={(event) => {
          event.dataTransfer.effectAllowed = "move";
          suppressVideoClickUntil.current = Number.POSITIVE_INFINITY;
          dragSourceRef.current = { key: item.key, folderId: parentId };
          setDraggedKey(item.key);
        }}
        onDragOver={(event) => handleDragOver(event, item, parentId)}
        onDragLeave={handleDragLeave}
        onDrop={(event) => handleDrop(event, item, parentId)}
        onDragEnd={() => { suppressVideoClickUntil.current = Date.now() + 250; clearDragState(); }}
      >
        <label className="library-video-select" title={`选择视频“${video.display_name}”`} onDragStart={(event) => { event.preventDefault(); event.stopPropagation(); }}>
          <input type="checkbox" aria-label={`选择视频“${video.display_name}”`} checked={selected} disabled={processingDisabled}
            onChange={(event) => setSelections((current) => ({
              ...current, [selectionKey]: event.target.checked ? [...(current[selectionKey] ?? []), video.id]
                : (current[selectionKey] ?? []).filter((id) => id !== video.id),
            }))} />
        </label>
        <button className="library-thumbnail-link" type="button" onClick={openVideo} aria-label={`打开视频“${video.display_name}”`} draggable={false}>
          <VideoThumbnail video={video} />
          {video.duration_ms > 0 && <span className="thumbnail-duration">{formatTime(video.duration_ms)}</span>}
        </button>
        {editingVideoName ? (
          <form className="library-video-rename" aria-label="重命名视频" onSubmit={(event) => {
            event.preventDefault();
            void saveVideoName();
          }} onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              if (!videoRenameSaving.current) setRenamingVideo(null);
            } else if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault();
          }} onDragStart={(event) => { event.preventDefault(); event.stopPropagation(); }}>
            <label className="field-label" htmlFor={`video-filename-${video.id}`}>文件名</label>
            <div className="library-video-rename-controls">
              <input ref={videoFilenameRef} id={`video-filename-${video.id}`} value={videoFilename} maxLength={200}
                disabled={videoRenameBusy} aria-invalid={Boolean(videoRenameError)}
                aria-describedby={videoRenameError ? `video-rename-error-${video.id}` : undefined}
                onChange={(event) => { setVideoFilename(event.target.value); setVideoRenameError(null); }} />
              <button className="icon-button" type="submit" title={renameBlockReason || "保存文件名"} aria-label="保存文件名"
                disabled={videoRenameBusy || Boolean(renameBlockReason) || !videoFilename.trim() || videoFilename.trim() === renamingVideo.filename}>
                {videoRenameBusy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
              </button>
              <button className="icon-button" type="button" title="取消重命名" aria-label="取消重命名"
                disabled={videoRenameBusy} onClick={() => setRenamingVideo(null)}><X size={16} /></button>
            </div>
            {videoRenameError && <p id={`video-rename-error-${video.id}`} className="field-error" role="alert">{videoRenameError}</p>}
            {renameBlockReason && !videoRenameError && <p className="field-error" role="status">{renameBlockReason}</p>}
          </form>
        ) : <button className="library-card-body library-card-link" type="button" onClick={openVideo} draggable={false}>
          <strong title={video.filename}>{video.display_name}</strong>
        </button>}
        <div className="library-video-labels">
            <span className={`status-badge status-${ready ? "ready" : video.status === "failed" ? "failed" : video.status === "processing" ? "processing" : "pending"}`} title={`${statusLabel(video.status)}${!ready ? ` · ${statusHint(video.status)}` : ""}`}>{statusLabel(video.status)}</span>
            <MarkerImportanceCounts counts={video.marker_importance_counts} />
        </div>
        <div className="library-card-actions">
          <button className="icon-button library-card-rename" type="button" title={renameBlockReason || `重命名视频“${video.display_name}”`}
            aria-label={`重命名视频“${video.display_name}”`} aria-expanded={editingVideoName}
            disabled={Boolean(renameBlockReason) || moving.has(video.id) || renamingVideo !== null}
            onClick={(event) => {
              if (Date.now() < suppressVideoClickUntil.current) return;
              videoRenameTriggerRef.current = event.currentTarget;
              setVideoFilename(video.filename);
              setVideoRenameError(null);
              setRenamingVideo(video);
            }}><Pencil size={16} /></button>
          <button className="icon-button library-card-process" type="button" title={`处理视频“${video.display_name}”`}
            aria-label={`处理视频“${video.display_name}”`} disabled={processingDisabled || editingVideoName}
            onClick={() => {
              if (Date.now() < suppressVideoClickUntil.current) return;
              setNotice(null);
              setSelections((current) => ({ ...current, [selectionKey]: selected ? current[selectionKey] : [video.id] }));
              setProcessingOptions((current) => ({ ...current, [selectionKey]: true }));
              const selector = parentId ? ".folder-window .library-processing" : ".library-inner .library-processing";
              document.querySelector(selector)?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
            }}><Sparkles size={16} /></button>
          <button className="icon-button danger-icon library-card-delete" type="button" title="移入回收站"
            aria-label={`删除视频“${video.display_name}”`} disabled={trashBusyKey !== null || editingVideoName}
            onClick={() => setRecycleTarget({ kind: "video", video })}>
            {trashBusyKey === `video-${video.id}` ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}
          </button>
          <span className="drag-handle" title={moving.has(video.id) ? "正在移动" : "拖动调整位置"}>{moving.has(video.id) ? <LoaderCircle className="spin" size={17} /> : <GripVertical size={17} />}</span>
          <ToolbarMenu label={`管理视频“${video.display_name}”`} icon={<MoreHorizontal size={18} />} disabled={moving.has(video.id) || editingVideoName}>
            <StudyShareExportMenuItem video={video} blockReason={renameBlockReason} />
            <VideoMoveMenu currentFolder={parseFolderId(libraryState.assignments[String(video.id)] ?? null)}
              onMove={(id) => void moveVideo(video.id, id === null ? "" : `folder-${id}`)} />
          </ToolbarMenu>
        </div>
        {dragFeedback(item)}
      </motion.article>
    );
  };

  const libraryReady = rootLibrary.data !== undefined;
  const operationErrorView = operationError && <div className="library-operation-error" role="alert"><AlertCircle size={17} /><span>{operationError.text}</span>
    <button className="secondary-button compact" onClick={operationError.retry}><RotateCcw size={14} />重试</button>
    <button className="icon-button" aria-label="关闭错误提示" title="关闭错误提示" onClick={() => setOperationError(null)}><X size={15} /></button></div>;

  return (
    <main ref={librarySurfaceRef} className="library-page has-warm-atmosphere" data-dragging={draggedKey !== null}>
      <WarmAtmosphere active={active && !suspended && !folderId && !renamingFolder && !isCreatingFolder && draggedKey === null} />
      <div className="library-scene" aria-hidden="true" />
      <LibraryFileImport enabled={active && !suspended && !folderId && !renamingFolder && !isCreatingFolder}>
      <div className="library-inner" ref={libraryBackgroundRef}>
        <div className="library-heading">
          <div className="library-heading-copy"><p className="character-tagline">收藏课程，温习重点。</p><h1 tabIndex={-1}>视频库</h1><span className="library-total">{videoList.length} 个视频 · {visibleFolders.length} 个文件夹</span></div>
          <div className="library-heading-actions">
            <Link className="primary-button" to="/process"><FileVideo size={17} />导入视频</Link>
            <Link className="secondary-button" to="/batch-process"><FolderOpen size={17} />导入文件夹</Link>
          </div>
          <CharacterAccent />
        </div>
        <div className="library-toolbar">
          <div className="library-breadcrumb"><span>本地视频库</span></div>
          <div className="library-toolbar-tools">
          <StudyShareImportButton key={scope} libraryId={libraryId} active={active && !suspended} />
          <label className="sort-control">
            <span>排序</span>
            <select value={sortMode} aria-label="视频库排序" onChange={(event) => setSortMode(event.target.value as "manual" | "name" | "time")}>
              <option value="manual">自定义位置</option>
              <option value="time">最近更新</option>
              <option value="name">名称 A-Z</option>
            </select>
            {sortMode === "name" ? <ArrowDownAZ size={15} /> : sortMode === "time" ? <Clock3 size={15} /> : <GripVertical size={15} />}
          </label>
          </div>
        </div>

        {(videos.isLoading || rootLibrary.isLoading) && <div className="library-state"><LoaderCircle className="spin" size={20} /> 正在读取本地视频库…</div>}
        {rootLibrary.isError && <div className="library-error"><LocalError label="文件夹列表" error={rootLibrary.error} />
          <button className="secondary-button compact" onClick={() => void rootLibrary.refetch()}><RotateCcw size={14} />重试</button></div>}
        {operationErrorView}
        {videos.isError && (
          <div className="error-panel" role="alert">
            <AlertCircle size={20} />
            <div>
              <strong>视频库暂时无法加载</strong>
              <p>{errorMessage(videos.error)}</p>
              <span>请确认本地服务已启动。</span>
              <button className="secondary-button compact" onClick={() => void videos.refetch()}><RotateCcw size={14} />重新读取</button>
            </div>
          </div>
        )}
        {libraryReady && <section className="library-folder-section" aria-labelledby="library-folders-title">
          <div className="library-section-heading"><h2 id="library-folders-title">文件夹 <span>{visibleFolders.length}</span></h2>
            <button className="text-button library-create-folder" type="button" onClick={() => { setCreateError(null); setIsCreatingFolder(true); }}><FolderPlus size={17} />新建文件夹</button>
          </div>
          {visibleFolders.length > 0 ? <div className="library-list library-folder-list">{rootItems.filter((item) => item.kind === "folder").map((item) => renderLibraryItem(item))}</div>
            : <p className="folder-section-empty">暂无文件夹</p>}
        </section>}
        {libraryReady && <div className="library-section-heading video-section-heading"><h2>视频 <span>{rootVideos.length}</span></h2></div>}
        {libraryReady && rootItems.length > 0 && processingControls(rootVideos)}
        {libraryReady && !videos.isLoading && !videos.isError && rootVideos.length === 0 && (
          <div className="empty-library">
            <FolderOpen size={24} />
            <strong>{videoList.length === 0 ? "视频库还没有视频" : "视频已收纳到文件夹"}</strong>
            <button className="secondary-button" onClick={() => onOpenPanel("/process")}><Upload size={16} />导入视频</button>
          </div>
        )}
        {libraryReady && rootVideos.length > 0 && (
          <div className="library-list library-grid-list">
            {rootItems.filter((item) => item.kind === "video").map((item) => renderLibraryItem(item))}
          </div>
        )}
      </div>
      </LibraryFileImport>
      {folderId && active && !suspended && !renamingFolder && !isCreatingFolder && (
        <FolderWindow viewKey={folderId} name={activeFolder?.name ?? "文件夹"} count={activeFolder?.videoCount ?? folderVideos.length}
          folderCount={folderItems.filter((item) => item.kind === "folder").length}
          breadcrumbs={folderBreadcrumbs}
          onNavigate={(id) => { clearDragState(); if (id) setFolderId(id); else closeFolder(); }}
          onCreateFolder={() => { setCreateError(null); setIsCreatingFolder(true); }}
          outsideDropLabel={activeFolder?.parentId ? "移至上一级文件夹" : "移至一级目录"}
          sortMode={folderSortMode} onSortChange={setFolderSortMode} onClose={closeFolder}
          canDropOutside={Boolean(draggedKey && dragSourceRef.current?.folderId === folderId)}
          showDragTextHints={showDragTextHints}
          onDropOutside={() => moveDraggedItemTo(activeFolder?.parentId ?? null)}
          onDropTo={moveDraggedItemTo}
          backgroundRef={libraryBackgroundRef} returnFocus={folderTriggerRef.current}
          initialScroll={folderScrolls.current[folderId] ?? 0}
          onScroll={(scroll) => { folderScrolls.current[folderId] = scroll; savedNavigation.current.folderScroll = scroll; saveLibraryNavigation(navigationKey, savedNavigation.current); }}>
          {operationErrorView}
          {folderItems.length > 0 && processingControls(folderVideos, folderId)}
          {(videos.isLoading || rootLibrary.isLoading || library.isLoading) ? (
            <div className="folder-window-empty"><LoaderCircle className="spin" size={24} /><span>正在读取文件夹…</span></div>
          ) : (library.isError || rootLibrary.isError || videos.isError) ? (
            <div>
              <LocalError label="文件夹" error={library.error ?? rootLibrary.error ?? videos.error} />
              <button type="button" className="icon-button" title="重新加载文件夹" aria-label="重新加载文件夹"
                onClick={() => { void library.refetch(); void rootLibrary.refetch(); void videos.refetch(); }}><RotateCcw size={16} /></button>
            </div>
          ) : !activeFolder ? (
            <div className="folder-window-empty"><FolderOpen size={24} /><strong>文件夹不存在</strong></div>
          ) : folderItems.length === 0 ? (
            <div className="folder-window-empty"><FolderOpen size={28} /><strong>这个文件夹还没有视频</strong></div>
          ) : (
            <div className="library-list library-grid-list folder-window-list">
              {folderItems.map((item) => renderLibraryItem(item, folderId))}
            </div>
          )}
          {notice && <p className="library-folder-notice" role="status"><CheckCircle2 size={16} />{notice}</p>}
        </FolderWindow>
      )}
      <AnimatePresence>
      {renamingFolder && active && !suspended && (
        <AnimatedOverlay key="rename" className="modal-backdrop" role="presentation" onClick={() => { if (!renameBusy) setRenamingFolder(null); }}>
          <motion.form initial={{ y: 4 }} animate={{ y: 0 }} exit={{ y: 4 }} transition={libraryMotion} ref={folderDialogRef} className="settings-dialog folder-dialog motion-surface" role="dialog" aria-modal="true" aria-labelledby="rename-folder-title" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => {
            if (event.key === "Escape" && !renameBusy) setRenamingFolder(null);
          }} onSubmit={(event) => {
            event.preventDefault();
            void saveFolderName();
          }}>
            <div className="dialog-heading"><h2 id="rename-folder-title">重命名文件夹</h2><button ref={folderDialogCloseRef} className="icon-button" type="button" disabled={renameBusy} onClick={() => setRenamingFolder(null)} aria-label="关闭重命名"><X size={18} /></button></div>
            <label className="field-group"><span className="field-label">文件夹名称</span><input autoFocus maxLength={200} value={renameName} disabled={renameBusy} onFocus={(event) => event.target.select()} onChange={(event) => {
              setRenameName(event.target.value);
              setRenameError(null);
            }} /></label>
            {renameError && <p className="field-error" role="alert">{renameError}</p>}
            <div className="process-actions"><button className="primary-button" type="submit" disabled={renameBusy || !renameName.trim() || renameName.trim() === renamingFolder.name}>{renameBusy ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} 保存</button><button className="secondary-button" type="button" disabled={renameBusy} onClick={() => setRenamingFolder(null)}>取消</button></div>
          </motion.form>
        </AnimatedOverlay>
      )}
      {isCreatingFolder && active && !suspended && (
        <AnimatedOverlay key="create" className="modal-backdrop" role="presentation" onClick={() => { if (!createBusy) setIsCreatingFolder(false); }}>
          <motion.form initial={{ y: 4 }} animate={{ y: 0 }} exit={{ y: 4 }} transition={libraryMotion} ref={folderDialogRef} className="settings-dialog folder-dialog motion-surface" role="dialog" aria-modal="true" aria-labelledby="folder-title" onClick={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); createFolder(); }}>
            <div className="dialog-heading"><div><p className="eyebrow">{activeFolder?.name ?? "一级目录"}</p><h2 id="folder-title">新建文件夹</h2></div><button ref={folderDialogCloseRef} className="icon-button" type="button" disabled={createBusy} onClick={() => setIsCreatingFolder(false)} aria-label="关闭"><X size={18} /></button></div>
            <label className="field-group"><span className="field-label">文件夹名称</span><input disabled={createBusy} autoFocus value={newFolderName} onChange={(event) => setNewFolderName(event.target.value)} placeholder="例如：高等数学 / 第一章" /></label>
            {createError && <p className="field-error" role="alert">{createError}</p>}
            <div className="process-actions"><button className="primary-button" type="submit" disabled={createBusy || !newFolderName.trim()}>{createBusy ? <LoaderCircle className="spin" size={15} /> : <FolderPlus size={15} />}创建</button><button className="secondary-button" type="button" disabled={createBusy} onClick={() => setIsCreatingFolder(false)}>取消</button></div>
          </motion.form>
        </AnimatedOverlay>
      )}
      </AnimatePresence>
      {recycleTarget && active && !suspended && <RecycleConfirmDialog
        kind={recycleTarget.kind}
        name={recycleTarget.kind === "folder" ? recycleTarget.folder.name : recycleTarget.video.display_name}
        backgroundRef={librarySurfaceRef} onConfirm={confirmRemoval} onClose={() => setRecycleTarget(null)} />}
      <AnimatePresence>{notice && active && !suspended && <AnimatedPopup duration={.18} className="toast" role="status"><CheckCircle2 size={16} /><span>{notice}</span>{canUndoRemoval && <button className="toast-action" type="button" onClick={() => void undoRemoval()} disabled={undoingRemoval}>{undoingRemoval ? "正在撤销…" : "撤销"}</button>}<button type="button" onClick={() => setNotice(null)} aria-label="关闭提示"><X size={15} /></button></AnimatedPopup>}</AnimatePresence>
    </main>
  );
}

const LIBRARY_STORAGE_KEY = "classroom-video-library";

function readLibraryState(key = LIBRARY_STORAGE_KEY): VideoLibraryState {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw) {
      const value = JSON.parse(raw) as Partial<VideoLibraryState>;
      return {
        folders: Array.isArray(value.folders) ? value.folders.filter((folder): folder is VideoLibraryFolder => Boolean(folder?.id && folder?.name)) : [],
        assignments: value.assignments && typeof value.assignments === "object" ? value.assignments : {},
        rootOrder: Array.isArray(value.rootOrder) ? value.rootOrder : [],
        folderOrders: value.folderOrders && typeof value.folderOrders === "object" ? value.folderOrders : {},
      };
    }
  } catch {
    // Ignore malformed local storage and start with an empty library layout.
  }
  return { folders: [], assignments: {}, rootOrder: [], folderOrders: {} };
}

type LibraryNavigation = { folderId: string | null; sortMode: "manual" | "name" | "time"; folderSortMode: "manual" | "name" | "time"; rootScroll: number; folderScroll: number };
function readLibraryNavigation(key: string): LibraryNavigation {
  const defaults: LibraryNavigation = { folderId: null, sortMode: "manual", folderSortMode: "manual", rootScroll: 0, folderScroll: 0 };
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? "{}");
    return { folderId: typeof saved.folderId === "string" && /^folder-\d+$/.test(saved.folderId) ? saved.folderId : null,
      sortMode: ["manual", "name", "time"].includes(saved.sortMode) ? saved.sortMode : "manual",
      folderSortMode: ["manual", "name", "time"].includes(saved.folderSortMode) ? saved.folderSortMode : "manual",
      rootScroll: Number.isFinite(saved.rootScroll) ? Math.max(0, saved.rootScroll) : 0,
      folderScroll: Number.isFinite(saved.folderScroll) ? Math.max(0, saved.folderScroll) : 0 };
  } catch { return defaults; }
}
function saveLibraryNavigation(key: string, state: LibraryNavigation) {
  try { sessionStorage.setItem(key, JSON.stringify(state)); } catch { /* Navigation also works without browser storage. */ }
}

function reconcileLibraryState(
  state: VideoLibraryState,
  videos: Video[],
  apiItems: Array<{ type: "folder" | "video"; folder?: LibraryFolder; video?: Video }> | undefined,
  parentId: number | null = null,
  rootItems?: Array<{ type: "folder" | "video"; folder?: LibraryFolder; video?: Video }>,
  ancestors: LibraryFolder[] = [],
): VideoLibraryState {
  const convertFolder = (folder: LibraryFolder): VideoLibraryFolder => ({
    id: `folder-${folder.id}`,
    name: folder.name,
    parentId: folder.parent_id == null ? null : `folder-${folder.parent_id}`,
    videoCount: folder.video_count,
    markerImportanceCounts: folder.marker_importance_counts,
    createdAt: Date.parse(folder.created_at ?? "") || Date.now(),
  });
  let folders = [...state.folders];
  const syncChildren = (items: NonNullable<typeof apiItems>, parent: string | null) => {
    const children = items.flatMap((item) => item.type === "folder" && item.folder
      ? [{ ...convertFolder(item.folder), parentId: parent }] : []);
    const ids = new Set(children.map((child) => child.id));
    const removed = new Set(folders.filter((folder) => (folder.parentId ?? null) === parent && !ids.has(folder.id)).map((folder) => folder.id));
    for (let previous = -1; previous !== removed.size;) {
      previous = removed.size;
      for (const folder of folders) if (folder.parentId && removed.has(folder.parentId)) removed.add(folder.id);
    }
    folders = [...folders.filter((folder) => !removed.has(folder.id) && !ids.has(folder.id)), ...children];
  };
  if (rootItems !== undefined) syncChildren(rootItems, null);
  for (const ancestor of ancestors) {
    folders = [...folders.filter((folder) => folder.id !== `folder-${ancestor.id}`), convertFolder(ancestor)];
  }
  if (parentId !== null && apiItems !== undefined) syncChildren(apiItems, `folder-${parentId}`);
  const apiOrder = (apiItems ?? []).flatMap((item) => {
    if (item.type === "folder" && item.folder) return [`folder-${item.folder.id}`];
    if (item.type === "video" && item.video) return [videoKey(item.video)];
    return [];
  });
  const known = new Set(videos.map((video) => videoKey(video)));
  const assignments = { ...state.assignments };
  for (const video of videos) {
    if (video.folder_id !== undefined) {
      assignments[String(video.id)] = video.folder_id == null ? null : `folder-${video.folder_id}`;
    } else if (!(String(video.id) in assignments)) {
      assignments[String(video.id)] = null;
    }
  }
  const rootOrder = mergeOrder(
    [...folders.filter((folder) => !folder.parentId).map(folderKey), ...videos.filter((video) => !assignments[String(video.id)]).map(videoKey)],
    state.rootOrder.length > 0 ? state.rootOrder : (rootItems ?? []).flatMap((item) =>
      item.folder ? [folderKey(convertFolder(item.folder))] : item.video ? [videoKey(item.video)] : []),
  );
  const folderOrders = Object.fromEntries(
    Object.entries(state.folderOrders).filter(([key]) => rootItems === undefined || folders.some((folder) => folder.id === key)),
  );
  for (const folder of folders) {
    const keys = [...folders.filter((child) => child.parentId === folder.id).map(folderKey),
      ...videos.filter((video) => assignments[String(video.id)] === folder.id).map(videoKey)];
    if (folderOrders[folder.id]) folderOrders[folder.id] = mergeOrder(keys, folderOrders[folder.id]);
  }
  if (parentId !== null && apiItems !== undefined) {
    const currentFolderKey = `folder-${parentId}`;
    const scopedKeys = apiOrder;
    folderOrders[currentFolderKey] = mergeOrder(scopedKeys,
      state.folderOrders[currentFolderKey]?.length ? state.folderOrders[currentFolderKey] : apiOrder);
  }
  return {
    ...state,
    folders,
    assignments,
    rootOrder: rootOrder.filter((key) => known.has(key) || key.startsWith("folder-")),
    folderOrders,
  };
}

function mergeOrder(keys: string[], order: string[]): string[] {
  const keySet = new Set(keys);
  return [...order.filter((key) => keySet.has(key)), ...keys.filter((key) => !order.includes(key))];
}

function videoKey(video: Video) { return `video-${video.id}`; }
function folderKey(folder: VideoLibraryFolder) { return folder.id; }
function parseFolderId(folderId: string | null): number | null {
  const match = folderId?.match(/^folder-(\d+)$/);
  return match ? Number(match[1]) : null;
}
function countFolderVideos(folderId: string, videos: Video[], state: VideoLibraryState) {
  return state.folders.find((folder) => folder.id === folderId)?.videoCount
    ?? videos.filter((video) => state.assignments[String(video.id)] === folderId).length;
}

function sortVideos(videos: Video[], mode: "manual" | "name" | "time", order: string[]) {
  if (mode === "name") return [...videos].sort((a, b) => a.display_name.localeCompare(b.display_name, "zh-CN"));
  if (mode === "time") return [...videos].sort((a, b) => videoTime(b) - videoTime(a));
  const rank = new Map(mergeOrder(videos.map(videoKey), order).map((key, index) => [key, index]));
  return [...videos].sort((a, b) => (rank.get(videoKey(a)) ?? 0) - (rank.get(videoKey(b)) ?? 0));
}

function sortFolders(folders: VideoLibraryFolder[], mode: "manual" | "name" | "time", order: string[]) {
  if (mode === "name") return [...folders].sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  if (mode === "time") return [...folders].sort((a, b) => b.createdAt - a.createdAt);
  const rank = new Map(mergeOrder(folders.map(folderKey), order).map((key, index) => [key, index]));
  return [...folders].sort((a, b) => (rank.get(folderKey(a)) ?? 0) - (rank.get(folderKey(b)) ?? 0));
}

type LibraryItem =
  | { kind: "folder"; key: string; folder: VideoLibraryFolder }
  | { kind: "video"; key: string; video: Video };

type DropIntent = {
  targetKey: string;
  mode: "folder" | "swap";
  edge: "before" | "after" | null;
};

function sortLibraryItems(items: LibraryItem[], mode: "manual" | "name" | "time", order: string[]) {
  if (mode === "name") {
    return [...items].sort((a, b) => libraryItemName(a).localeCompare(libraryItemName(b), "zh-CN"));
  }
  if (mode === "time") {
    return [...items].sort((a, b) => libraryItemTime(b) - libraryItemTime(a));
  }
  const rank = new Map(mergeOrder(items.map((item) => item.key), order).map((key, index) => [key, index]));
  return [...items].sort((a, b) => (rank.get(a.key) ?? 0) - (rank.get(b.key) ?? 0));
}

function libraryItemName(item: LibraryItem) {
  return item.kind === "folder" ? item.folder.name : item.video.display_name;
}

function libraryItemTime(item: LibraryItem) {
  return item.kind === "folder" ? item.folder.createdAt : videoTime(item.video);
}

function videoTime(video: Video) {
  return video.source_mtime_ns ? video.source_mtime_ns / 1_000_000 : video.id;
}


function statusLabel(status: string) {
  return {
    uploaded: "待处理",
    processing: "处理中",
    transcribed: "已转写",
    review_ready: "可复习",
    failed: "处理失败",
  }[status] ?? status;
}

function statusHint(status: string) {
  if (status === "failed") return "AI 未完成或处理失败";
  if (status === "processing") return "等待处理完成";
  if (status === "transcribed") return "还需生成重点";
  return "完成处理后可复习";
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请检查本地服务。";
}
