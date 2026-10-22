import { afterEach, expect, it, vi } from "vitest";
import { downloadStudyShare, importStudyShare, previewStudyShare } from "./client";
import type { StudySharePreview, Video } from "../types";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("uploads unchanged package bytes and pins import to the preview and library", async () => {
  const file = new File(["bytes"], "课堂.studyshare");
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ can_import: true }) });
  vi.stubGlobal("fetch", fetch);
  await previewStudyShare(file);
  expect(fetch).toHaveBeenCalledWith("/api/study-shares/preview", expect.objectContaining({ method: "POST", body: file }));
  await importStudyShare(file, { package_hash: "abc", video_id: 7, library_id: "library + &" } as StudySharePreview);
  const [url, options] = fetch.mock.calls[1];
  const query = new URL(url, "http://local").searchParams;
  expect(query.get("package_hash")).toBe("abc");
  expect(query.get("video_id")).toBe("7");
  expect(query.get("library_id")).toBe("library + &");
  expect(options.body).toBe(file);
});

it("returns server validation errors and never downloads an error body", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: "视频内容已变化" }) }));
  await expect(downloadStudyShare({ id: 7 } as Video)).rejects.toThrow("视频内容已变化");
  await expect(previewStudyShare(new File(["x"], "x.studyshare"))).rejects.toThrow("视频内容已变化");
});

it("downloads a sanitized filename and releases the temporary object URL", async () => {
  vi.useFakeTimers();
  const blob = new Blob(["share"]);
  const create = vi.fn(() => "blob:fixture"), revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => blob }));
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    expect(this.download).toBe("课程__1.studyshare");
    expect(this.href).toBe("blob:fixture");
  });
  try {
    await downloadStudyShare({ id: 7, display_name: "课程:/1" } as Video);
    expect(click).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith(blob);
    expect(document.querySelector("a")).toBeNull();
    vi.runAllTimers();
    expect(revoke).toHaveBeenCalledWith("blob:fixture");
  } finally { vi.useRealTimers(); }
});
