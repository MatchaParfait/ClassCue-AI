import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import type { AiSettings } from "../types";
import { SettingsPage } from "./SettingsPage";

vi.mock("../api/client");
let container: HTMLDivElement, root: Root, client: QueryClient;
let saved: AiSettings[];
let selected: string;
let onConfigurationSaved: ReturnType<typeof vi.fn>;
const snapshot = (): AiSettings => ({
  ...(saved.find((item) => item.connection_id === selected) ?? {
    connection_id: "", connection_name: "", provider: "openai_compatible",
    base_url: "", model: "", output_tokens: 16384, token_parameter: "max_tokens",
    json_mode: true, timeout_seconds: 90, max_retries: 3, has_api_key: false,
  }),
  connections: saved.map((item) => ({
    id: item.connection_id!, name: item.connection_name!, model: item.model,
    base_url: item.base_url, has_api_key: item.has_api_key,
  })),
});

beforeEach(async () => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  saved = ["first", "second"].map((id) => ({
    connection_id: id, connection_name: id === "first" ? "日常分析" : "精细分析",
    provider: "openai_compatible", base_url: "https://same.test/v1", model: `${id}-model`,
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 60, max_retries: 2, has_api_key: true,
  }));
  selected = "first";
  onConfigurationSaved = vi.fn();
  vi.mocked(api.getAiSettings).mockImplementation(async () => snapshot());
  vi.mocked(api.getLibraryStatus).mockResolvedValue({ data_root: "fixture", video_count: 0, database_path: "", temp_audio_dir: "" });
  vi.mocked(api.activateAiConnection).mockImplementation(async (id) => { selected = id; return snapshot(); });
  vi.mocked(api.createAiConnection).mockImplementation(async (name, sourceId, settings, credentials) => {
    const source = saved.find((item) => item.connection_id === sourceId);
    const added = { ...(source ?? saved[0]), ...settings, connection_id: "created", connection_name: name,
      has_api_key: Boolean(credentials?.api_key || (source?.has_api_key && !credentials?.remove_key)) };
    saved.push(added);
    selected = added.connection_id;
    return snapshot();
  });
  vi.mocked(api.renameAiConnection).mockImplementation(async (id, name) => {
    saved = saved.map((item) => item.connection_id === id ? { ...item, connection_name: name } : item);
    return snapshot();
  });
  vi.mocked(api.deleteAiConnection).mockImplementation(async (id) => {
    saved = saved.filter((item) => item.connection_id !== id);
    selected = saved[0]?.connection_id ?? "";
    return snapshot();
  });
  vi.mocked(api.saveAiSettings).mockImplementation(async (settings, credentials) => {
    saved = saved.map((item) => item.connection_id === settings.connection_id
      ? { ...settings, has_api_key: credentials?.remove_key ? false : Boolean(credentials?.api_key || item.has_api_key) } : item);
    return snapshot();
  });
  vi.mocked(api.saveAiApiKey).mockImplementation(async () => snapshot());
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => root.render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/settings?section=ai"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <SettingsPage onConfigurationSaved={onConfigurationSaved} />
    </MemoryRouter>
  </QueryClientProvider>));
  await until(() => expect(container.querySelector('select[aria-label="已保存连接"]')).not.toBeNull());
});

afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); });
  container.remove();
  vi.restoreAllMocks();
});

async function until(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    check();
  });
}
function button(name: string) {
  const result = [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find((item) => item.getAttribute("aria-label") === name || item.textContent?.trim() === name);
  expect(result, name).toBeDefined();
  return result!;
}
function click(name: string) { act(() => button(name).click()); }
function input(label: string) {
  return [...container.querySelectorAll("label")].find((node) => node.textContent === label)!.querySelector("input")!;
}
function setInput(node: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function select(id: string) {
  const node = container.querySelector<HTMLSelectElement>('select[aria-label="已保存连接"]')!;
  act(() => { node.value = id; node.dispatchEvent(new Event("change", { bubbles: true })); });
}

it("switches saved connections immediately and clears key plaintext and old model results", async () => {
  vi.mocked(api.revealAiApiKey).mockResolvedValue("first-secret");
  click("显示 API Key");
  await until(() => expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("first-secret"));
  select("second");
  await until(() => expect(input("模型名称").value).toBe("second-model"));
  expect(api.activateAiConnection).toHaveBeenCalledWith("second");
  expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("********");
  expect(api.revealAiApiKey).toHaveBeenCalledWith("https://same.test/v1", expect.any(AbortSignal), "first");
  expect(client.getQueryData<AiSettings>(["settings", "ai"])?.connection_id).toBe("second");
  expect(onConfigurationSaved).not.toHaveBeenCalled();
});

it("keeps drafts on cancel and saves configuration and pending key before switching", async () => {
  setInput(input("模型名称"), "draft-model");
  setInput(container.querySelector('input[aria-label="API Key"]')!, "draft-key");
  select("second");
  expect(container.querySelector('[role="dialog"]')?.textContent).toContain("保存当前修改");
  expect(api.activateAiConnection).not.toHaveBeenCalled();
  click("取消");
  expect(input("模型名称").value).toBe("draft-model");
  select("second");
  click("保存并继续");
  await until(() => expect(input("模型名称").value).toBe("second-model"));
  expect(api.saveAiSettings).toHaveBeenCalledWith(expect.objectContaining({ connection_id: "first", model: "draft-model" }), { api_key: "draft-key" });
  expect(api.saveAiApiKey).not.toHaveBeenCalled();
  expect(api.activateAiConnection).toHaveBeenCalledTimes(1);
  expect(saved[0].model).toBe("draft-model");
  expect(onConfigurationSaved).not.toHaveBeenCalled();
});

it("preserves the draft and dialog when save fails, and supports discard without saving", async () => {
  vi.mocked(api.saveAiSettings).mockRejectedValueOnce(new Error("保存失败"));
  setInput(input("模型名称"), "keep-me");
  select("second");
  click("保存并继续");
  await until(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("保存失败"));
  expect(input("模型名称").value).toBe("keep-me");
  expect(api.activateAiConnection).not.toHaveBeenCalled();
  click("放弃修改并继续");
  await until(() => expect(input("模型名称").value).toBe("second-model"));
  expect(saved[0].model).toBe("first-model");
  expect(container.querySelector('[role="dialog"]')).toBeNull();
});

it("creates and copies locally until a complete connection is saved, then supports delete", async () => {
  expect(input("输出 Token 上限").value).toBe("2048");
  click("新增");
  const name = input("连接名称");
  expect(document.activeElement).toBe(name);
  expect(api.createAiConnection).not.toHaveBeenCalled();
  expect(input("输出 Token 上限").value).toBe("16384");
  expect(input("请求超时（秒）").value).toBe("120");
  expect(input("最大重试次数").value).toBe("5");
  expect(container.querySelector<HTMLSelectElement>('select[aria-label="思考强度"]')?.value).toBe("high");
  expect(selected).toBe("first");
  setInput(name, "备用");
  setInput(container.querySelector("#ai-base-url")!, "https://new.test/v1");
  setInput(input("模型名称"), "new-model");
  setInput(container.querySelector('input[aria-label="API Key"]')!, "new-key");
  click("保存连接");
  await until(() => expect(snapshot().connection_name).toBe("备用"));
  expect(api.createAiConnection).toHaveBeenCalledWith("备用", undefined,
    expect.objectContaining({ base_url: "https://new.test/v1", model: "new-model", output_tokens: 16384,
      timeout_seconds: 120, max_retries: 5, reasoning_effort: "high" }), { api_key: "new-key" });
  click("删除当前连接");
  expect(api.deleteAiConnection).not.toHaveBeenCalled();
  click("确认删除连接");
  await until(() => expect(input("模型名称").value).toBe("first-model"));
  expect(api.deleteAiConnection).toHaveBeenCalledWith("created");
  click("复制当前连接");
  expect(input("连接名称").value).toBe("日常分析 副本");
  expect(selected).toBe("first");
  click("保存连接");
  await until(() => expect(api.createAiConnection).toHaveBeenLastCalledWith("日常分析 副本", "first", expect.any(Object), undefined));
  expect(snapshot().has_api_key).toBe(true);
});

it("does not apply delayed switching twice and does not lose edits when activation fails", async () => {
  let fail!: (error: Error) => void;
  vi.mocked(api.activateAiConnection).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
  select("second");
  expect(container.querySelector<HTMLFieldSetElement>(".ai-connection-editor")?.disabled).toBe(true);
  expect(api.activateAiConnection).toHaveBeenCalledTimes(1);
  await act(async () => { fail(new Error("切换失败")); });
  expect(input("模型名称").value).toBe("first-model");
  expect(container.querySelector<HTMLSelectElement>('select[aria-label="已保存连接"]')?.value).toBe("first");
  expect(container.querySelector<HTMLFieldSetElement>(".ai-connection-editor")?.disabled).toBe(false);
});

it("shows three effort levels below the model, sends high for unset settings and preserves saved medium", async () => {
  const effort = container.querySelector<HTMLSelectElement>('select[aria-label="思考强度"]')!;
  expect(effort.closest("details")).toBeNull();
  expect([...effort.options].map((option) => option.value)).toEqual(["low", "medium", "high"]);
  expect(effort.value).toBe("high");
  expect(container.querySelector("#ai-reasoning-hint")?.textContent).toContain("DeepSeek 4.1 Flash");
  expect(container.querySelector("#ai-reasoning-hint")?.textContent).toContain("建议最低使用 Medium");
  expect(input("模型名称").compareDocumentPosition(effort) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  setInput(input("连接名称"), "默认 High");
  click("保存连接");
  await until(() => expect(api.saveAiSettings).toHaveBeenCalledWith(
    expect.objectContaining({ reasoning_effort: "high" }), undefined));
  act(() => { effort.value = "medium"; effort.dispatchEvent(new Event("change", { bubbles: true })); });
  click("保存连接");
  await until(() => expect(saved[0].reasoning_effort).toBe("medium"));
  select("second");
  await until(() => expect(input("模型名称").value).toBe("second-model"));
  expect(effort.value).toBe("high");
  select("first");
  await until(() => expect(input("模型名称").value).toBe("first-model"));
  expect(effort.value).toBe("medium");
});

it("saves the API concurrency per connection and includes it in draft changes", async () => {
  const concurrency = input("API 并发上限");
  expect(concurrency.closest("details")).toBeNull();
  expect(concurrency.value).toBe("1");
  expect(concurrency.min).toBe("1");
  expect(concurrency.max).toBe("16");
  setInput(concurrency, "4");
  expect(button("保存连接").disabled).toBe(false);
  click("保存连接");
  await until(() => expect(saved[0].request_concurrency).toBe(4));
  expect(api.saveAiSettings).toHaveBeenCalledWith(expect.objectContaining({ request_concurrency: 4 }), undefined);
  select("second");
  await until(() => expect(concurrency.value).toBe("1"));
  select("first");
  await until(() => expect(concurrency.value).toBe("4"));
  setInput(concurrency, "8");
  click("放弃修改");
  expect(concurrency.value).toBe("4");
  expect(button("保存连接").disabled).toBe(true);
});

it.each(["0", "17", "2.5", ""])("does not save invalid API concurrency %s", (value) => {
  const concurrency = input("API 并发上限");
  setInput(concurrency, value);
  expect(concurrency.getAttribute("aria-invalid")).toBe("true");
  expect(button("保存连接").disabled).toBe(true);
  expect(api.saveAiSettings).not.toHaveBeenCalled();
});

it("deletes every connection, keeps the empty state after refresh and saves a new connection", async () => {
  click("删除当前连接");
  click("确认删除连接");
  await until(() => expect(input("模型名称").value).toBe("second-model"));
  expect(button("删除当前连接").disabled).toBe(false);
  click("删除当前连接");
  expect(container.querySelector('[role="dialog"]')?.textContent).toContain("将不再有已保存连接");
  click("取消");
  expect(api.deleteAiConnection).toHaveBeenCalledTimes(1);
  click("删除当前连接");
  click("确认删除连接");
  await until(() => expect(container.textContent).toContain("暂无已保存连接"));
  expect(button("删除当前连接").disabled).toBe(true);
  expect(button("复制当前连接").disabled).toBe(true);
  expect(container.querySelector('input[aria-label="API Key"]')).toBeNull();
  expect(container.textContent).not.toContain("连接已保存");
  await act(async () => { await client.invalidateQueries({ queryKey: ["settings", "ai"] }); });
  await until(() => expect(client.getQueryData<AiSettings>(["settings", "ai"])?.connections).toEqual([]));
  click("新增");
  expect(input("输出 Token 上限").value).toBe("16384");
  expect(input("模型名称").value).toBe("");
  expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("");
  setInput(input("连接名称"), "重新添加");
  setInput(container.querySelector("#ai-base-url")!, "https://new.test/v1");
  setInput(input("模型名称"), "new-model");
  setInput(container.querySelector('input[aria-label="API Key"]')!, "new-key");
  click("保存连接");
  await until(() => expect(snapshot().connection_name).toBe("重新添加"));
  expect(button("删除当前连接").disabled).toBe(false);
  expect(snapshot().connections).toHaveLength(1);
});

it("saves name, model and key in one request and can revert all pending changes", async () => {
  setInput(input("模型名称"), "unsaved");
  setInput(container.querySelector('input[aria-label="API Key"]')!, "unsaved-key");
  setInput(input("连接名称"), "新名称");
  click("保存连接");
  await until(() => expect(api.saveAiSettings).toHaveBeenCalledWith(expect.objectContaining({
    connection_name: "新名称", model: "unsaved",
  }), { api_key: "unsaved-key" }));
  await until(() => expect(button("保存连接").disabled).toBe(true));
  expect(onConfigurationSaved).toHaveBeenCalledTimes(1);
  expect(input("模型名称").value).toBe("unsaved");
  expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("********");
  setInput(input("模型名称"), "discard-me");
  click("放弃修改");
  expect(input("模型名称").value).toBe("unsaved");
});

it("keeps a pending key attached to its original connection across an external settings refresh", async () => {
  setInput(container.querySelector('input[aria-label="API Key"]')!, "first-account-draft");
  selected = "second";
  act(() => client.setQueryData(["settings", "ai"], snapshot()));
  await until(() => expect(input("模型名称").value).toBe("first-model"));
  expect(container.querySelector<HTMLSelectElement>('select[aria-label="已保存连接"]')?.value).toBe("first");
  expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("first-account-draft");
  click("保存连接");
  await until(() => expect(api.saveAiSettings).toHaveBeenCalledWith(
    expect.objectContaining({ connection_id: "first" }), { api_key: "first-account-draft" }));
});

it("uses a pasted draft key to fetch models before a new connection has a model or has been saved", async () => {
  vi.mocked(api.getAiRemoteModels).mockResolvedValue(["fetched-model"]);
  click("新增");
  setInput(input("连接名称"), "新服务");
  setInput(container.querySelector("#ai-base-url")!, "https://new.test/v1");
  setInput(container.querySelector('input[aria-label="API Key"]')!, "pasted-key");
  expect(input("模型名称").value).toBe("");
  expect(button("获取模型").disabled).toBe(false);
  click("获取模型");
  await until(() => expect(container.querySelector(".models-dialog")).not.toBeNull());
  expect(api.getAiRemoteModels).toHaveBeenCalledWith("https://new.test/v1", undefined,
    expect.objectContaining({ api_key: "pasted-key", settings: expect.objectContaining({ model: "" }) }));
  expect(api.saveAiSettings).not.toHaveBeenCalled();
  expect(api.createAiConnection).not.toHaveBeenCalled();
  click("fetched-model");
  expect(input("模型名称").value).toBe("fetched-model");
  click("取消新增");
  expect(input("模型名称").value).toBe("first-model");
  expect(selected).toBe("first");
});

it("presets fill only the address and never send the previous endpoint's key", async () => {
  setInput(container.querySelector('input[aria-label="API Key"]')!, "do-not-transfer");
  click("DeepSeek");
  expect(container.querySelector<HTMLInputElement>("#ai-base-url")?.value).toBe("https://api.deepseek.com");
  expect(input("模型名称").value).toBe("first-model");
  expect(container.querySelector<HTMLInputElement>('input[aria-label="API Key"]')?.value).toBe("");
  expect(button("获取模型").disabled).toBe(true);
  expect(container.textContent).not.toContain("服务类型");
  expect(api.activateAiConnection).not.toHaveBeenCalled();
  click("放弃修改");
  expect(container.querySelector<HTMLInputElement>("#ai-base-url")?.value).toBe("https://same.test/v1");
});

it("stages key removal and supports undo before saving the whole connection", async () => {
  click("移除已保存的 API Key");
  expect(button("获取模型").disabled).toBe(true);
  expect(api.deleteAiApiKey).not.toHaveBeenCalled();
  click("恢复已保存的 API Key");
  expect(button("获取模型").disabled).toBe(false);
  expect(button("保存连接").disabled).toBe(true);
  click("移除已保存的 API Key");
  click("保存连接");
  await until(() => expect(api.saveAiSettings).toHaveBeenCalledWith(
    expect.objectContaining({ connection_id: "first" }), { remove_key: true }));
});
