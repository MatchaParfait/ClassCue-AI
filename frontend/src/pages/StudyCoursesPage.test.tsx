import { act, type ComponentProps } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StudyCoursesPage } from "./StudyCoursesPage";
import * as api from "../api/studyMaterials";

vi.mock("../api/studyMaterials", async original => ({
  ...await original<typeof import("../api/studyMaterials")>(),
  getStudyCourses: vi.fn(), getStudyCourseFolders: vi.fn(), getCourseForFolder: vi.fn(),
  getStudyDocuments: vi.fn(), getCourseVideoQuestions: vi.fn(), createStudyCourse: vi.fn(), studyWrite: vi.fn(),
}));
vi.mock("./StudyMaterialsPage", () => ({
  StudyMaterialsPage: ({ courseId }: { courseId: string }) => <div data-course-reader={courseId}>上传资料阅读器</div>,
}));
vi.mock("../components/DshQuestionActions", () => ({
  DshQuestionActions: ({ questionId }: { questionId: string }) => <button data-discussion={questionId}>DSH 讨论</button>,
}));

let root: Root, container: HTMLDivElement, cache: QueryClient;
const database: api.StudyCourse = { id: "db", name: "数据库", folder_id: 1, document_count: 3, question_count: 5 };
const algorithms: api.StudyCourse = { id: "algo", name: "算法", folder_id: 2, document_count: 2, question_count: 4 };
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.getStudyCourses).mockResolvedValue([database, algorithms]);
  vi.mocked(api.getStudyCourseFolders).mockResolvedValue([
    { id: 1, name: "数据库", parent_id: null, course_id: "db" },
    { id: 2, name: "算法", parent_id: null, course_id: "algo" },
  ]);
  vi.mocked(api.getStudyDocuments).mockResolvedValue([]);
  vi.mocked(api.getCourseVideoQuestions).mockResolvedValue([
    { id: "video-q", course_id: "db", version: 1, title: "视频中的情景题", source_name: "第一讲", marker_id: 3, video_id: 5,
      start_ms: 1000, capture_id: "capture" },
  ]);
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(async () => { await act(async () => root.unmount()); cache.clear(); container.remove(); });
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
function NavigationProbe() {
  const location = useLocation(), navigate = useNavigate();
  return <div data-current-path={location.pathname} data-return-to={location.state?.returnTo ?? ""}>
    <button onClick={() => navigate(-1)}>测试返回来源</button>
  </div>;
}
type InitialEntry = NonNullable<ComponentProps<typeof MemoryRouter>["initialEntries"]>[number];
async function mount(path: InitialEntry = "/study-materials", previous?: string) {
  await act(async () => root.render(<QueryClientProvider client={cache}><MemoryRouter initialEntries={previous ? [previous, path] : [path]} initialIndex={previous ? 1 : 0}>
    <StudyCoursesPage scope="test" libraryId="library" />
    <NavigationProbe />
  </MemoryRouter></QueryClientProvider>));
  await settle(); await settle();
}
async function click(label: string) {
  const node = [...container.querySelectorAll("button")].find(button => button.textContent?.includes(label) || button.getAttribute("aria-label") === label)!;
  expect(node).toBeTruthy();
  await act(async () => node.click()); await settle();
}

it("shows course folders first and opens a reader scoped to the chosen course", async () => {
  vi.mocked(api.getStudyCourseFolders).mockResolvedValue([
    { id: 1, name: "数据库", parent_id: null, course_id: "db" },
    { id: 2, name: "算法", parent_id: null, course_id: "algo" },
    { id: 3, name: "新课程", parent_id: null, course_id: null },
    { id: 4, name: "第一周", parent_id: 3, course_id: null },
  ]);
  await mount();
  expect(container.querySelectorAll(".study-course-row")).toHaveLength(2);
  expect(container.querySelector("[data-course-reader]")).toBeNull();
  await click("新建课程资料夹");
  const selector = container.querySelector<HTMLSelectElement>('[aria-label="绑定课程视频文件夹"]')!;
  expect([...selector.options].map(option => option.value)).toEqual(["", "3"]);
  await click("取消创建");
  await click("数据库");
  expect(container.querySelector("[data-course-reader]")?.getAttribute("data-course-reader")).toBe("db");
  expect(container.textContent).not.toContain("算法");
  expect(api.getCourseVideoQuestions).not.toHaveBeenCalled();
  await click("返回全部课程");
  await click("算法");
  expect(container.querySelector("[data-course-reader]")?.getAttribute("data-course-reader")).toBe("algo");
});

it("reads the course's video questions and uses the shared discussion identity", async () => {
  await mount("/study-materials?course=db&tab=video");
  expect(api.getCourseVideoQuestions).toHaveBeenCalledWith("db");
  expect(container.querySelector("[data-discussion]")?.getAttribute("data-discussion")).toBe("video-q");
  expect(container.querySelector('a[href="/review/5?start_ms=1000&library_id=library"]')).not.toBeNull();
  expect(container.querySelector(".study-course-reader")?.hasAttribute("hidden")).toBe(true);
});

it("reuses a parent course when entering materials from a nested video folder", async () => {
  vi.mocked(api.getCourseForFolder).mockResolvedValue(database);
  await mount("/study-materials?folder=8");
  expect(api.getCourseForFolder).toHaveBeenCalledWith(8);
  expect(api.createStudyCourse).not.toHaveBeenCalled();
  expect(container.querySelector("[data-course-reader]")?.getAttribute("data-course-reader")).toBe("db");
});

it("creates an explicit course binding on first entry from a video folder", async () => {
  vi.mocked(api.getCourseForFolder).mockResolvedValue(null);
  vi.mocked(api.createStudyCourse).mockResolvedValue(database);
  await mount("/study-materials?folder=1");
  expect(api.createStudyCourse).toHaveBeenCalledWith({ folder_id: 1 });
  expect(container.querySelector("[data-course-reader]")).not.toBeNull();
});

it("keeps legacy documents outside courses until the user assigns them", async () => {
  vi.mocked(api.getStudyDocuments).mockResolvedValue([
    { id: "legacy", name: "旧试卷.pdf", format: "pdf", state: "ready", reason: "", page_count: 2,
      question_count: 3, deleted: false, reading: {}, course_id: null },
  ]);
  vi.mocked(api.studyWrite).mockResolvedValue({});
  await mount(); await click("待归属资料");
  expect(container.textContent).toContain("旧试卷.pdf");
  const selector = container.querySelector<HTMLSelectElement>('[aria-label="资料目标课程"]')!;
  await act(async () => { selector.value = "db"; selector.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  await click("移入课程");
  expect(api.studyWrite).toHaveBeenCalledWith("/api/study-documents/legacy/course", "PUT", { course_id: "db" });
});

it("preserves the original workbench return through course navigation without adding internal back steps", async () => {
  await mount({ pathname: "/study-materials", state: { workbench: true, returnTo: "/process?videoId=7" } }, "/process?videoId=7");
  await click("数据库"); await click("视频例题"); await click("上传资料"); await click("返回全部课程");
  expect(container.querySelector("[data-return-to]")?.getAttribute("data-return-to")).toBe("/process?videoId=7");
  await click("测试返回来源");
  expect(container.querySelector("[data-current-path]")?.getAttribute("data-current-path")).toBe("/process");
});

it("retries the actual failed video-question request", async () => {
  vi.mocked(api.getCourseVideoQuestions).mockRejectedValueOnce(new Error("读取例题失败"));
  await mount("/study-materials?course=db&tab=video");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("读取例题失败");
  await click("重新读取");
  expect(api.getCourseVideoQuestions).toHaveBeenCalledTimes(2);
  expect(container.querySelector("[data-discussion]")).not.toBeNull();
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("retries resolving an incoming course folder after a transient failure", async () => {
  vi.mocked(api.getCourseForFolder).mockRejectedValueOnce(new Error("目录暂不可用")).mockResolvedValue(database);
  await mount({ pathname: "/study-materials", search: "?folder=8", state: { workbench: true, returnTo: "/library/folder/folder-8" } });
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("目录暂不可用");
  await click("重新读取");
  expect(api.getCourseForFolder).toHaveBeenCalledTimes(2);
  expect(container.querySelector("[data-course-reader]")?.getAttribute("data-course-reader")).toBe("db");
  expect(container.querySelector("[data-return-to]")?.getAttribute("data-return-to")).toBe("/library/folder/folder-8");
});

it("lets users correct an existing folder binding and refreshes the course's video questions", async () => {
  vi.mocked(api.getStudyCourseFolders).mockResolvedValue([
    { id: 1, name: "数据库", parent_id: null, course_id: "db" },
    { id: 2, name: "算法", parent_id: null, course_id: "algo" },
    { id: 3, name: "数据库新目录", parent_id: null, course_id: null },
    { id: 4, name: "第一周", parent_id: 3, course_id: null },
  ]);
  vi.mocked(api.studyWrite).mockImplementation(async () => {
    vi.mocked(api.getStudyCourses).mockResolvedValue([{ ...database, name: "数据库新目录", folder_id: 3 }, algorithms]);
    return {};
  });
  await mount("/study-materials?course=db&tab=video");
  await click("更改关联");
  const selector = container.querySelector<HTMLSelectElement>('[aria-label="重新绑定视频文件夹"]')!;
  expect(selector.value).toBe("1");
  expect([...selector.options].map(option => option.value)).toEqual(["", "1", "3"]);
  await act(async () => { selector.value = "3"; selector.dispatchEvent(new Event("change", { bubbles: true })); });
  await click("保存关联");
  expect(api.studyWrite).toHaveBeenCalledWith("/api/study-courses/db/folder", "PUT", { folder_id: 3 });
  expect(api.getCourseVideoQuestions).toHaveBeenCalledTimes(2);
  expect(container.querySelector('a[href="/library/folder/folder-3"]')).not.toBeNull();
  expect(container.querySelector('[aria-label="重新绑定视频文件夹"]')).toBeNull();
});

it("keeps the old binding and selected correction after a failed save, and allows cancelling", async () => {
  vi.mocked(api.getStudyCourses).mockResolvedValue([{ ...database, folder_id: 4 }, algorithms]);
  vi.mocked(api.getStudyCourseFolders).mockResolvedValue([
    { id: 1, name: "数据库", parent_id: null, course_id: null },
    { id: 3, name: "数据库新目录", parent_id: null, course_id: null },
    { id: 4, name: "第一周", parent_id: 1, course_id: "db" },
    { id: 5, name: "第二周", parent_id: 1, course_id: null },
  ]);
  vi.mocked(api.studyWrite).mockRejectedValue(new Error("目录暂时不可用"));
  await mount("/study-materials?course=db");
  await click("更改关联");
  const selector = container.querySelector<HTMLSelectElement>('[aria-label="重新绑定视频文件夹"]')!;
  expect(selector.value).toBe("4");
  expect([...selector.options].map(option => option.value)).toEqual(["", "1", "3", "4"]);
  await act(async () => { selector.value = "3"; selector.dispatchEvent(new Event("change", { bubbles: true })); });
  await click("保存关联");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("目录暂时不可用");
  expect(selector.value).toBe("3");
  expect(container.querySelector('a[href="/library/folder/folder-4"]')).not.toBeNull();
  await click("取消更改关联");
  expect(container.querySelector('[aria-label="重新绑定视频文件夹"]')).toBeNull();
  expect(api.studyWrite).toHaveBeenCalledTimes(1);
});
