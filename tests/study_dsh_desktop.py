"""Native ClassCue -> DSH acceptance using synthetic content and an isolated profile.

Install the plugin into a Harness home under this checkout's .runtime first.
No model message is sent. All evidence and disposable app data stay in .runtime.
"""

import argparse
import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from classcue.dsh_pipe import PipeClient


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def isolated(path):
    resolved = path.resolve()
    if not resolved.is_relative_to((ROOT / ".runtime").resolve()):
        raise ValueError("Acceptance paths must be inside this checkout's .runtime")
    return resolved


def ipc(project, action):
    state = json.loads((project / ".runtime/desktop_instance.json").read_text())
    request = Request(
        f"http://127.0.0.1:{state['port']}/command",
        data=json.dumps({"action": action}).encode(),
        headers={"Authorization": f"Bearer {state['token']}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=3) as response:
        return json.load(response)


async def until(read, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = read()
            if asyncio.iscoroutine(value):
                value = await value
            if predicate(value):
                return value
        except Exception as error:
            last_error = type(error).__name__
        await asyncio.sleep(.2)
    raise AssertionError(f"Acceptance timed out ({last_error or 'condition not met'})")


def pdf_content():
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(600, 800))
    for number, text in enumerate((
        "3. Shared scenario: R(A,B,C). (1) Find candidate keys.",
        "3. Continued: F={A->B, B->C}. (2) Explain the normal form.",
    ), 1):
        pdf.drawString(65, 650, text)
        pdf.rect(65, 520, 430, 100)
        pdf.drawString(65, 40, f"Synthetic paper / {number}")
        pdf.showPage()
    pdf.save()
    return output.getvalue()


async def main(args):
    harness_home = isolated(args.harness_home)
    electron_profile = isolated(args.electron_profile)
    output = ROOT / ".runtime/study-dsh-desktop"
    output.mkdir(parents=True, exist_ok=True)
    prepared = subprocess.run(
        [str(args.python), "tests/fixtures/desktop_application.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    fixture = json.loads(prepared.stdout.strip().splitlines()[-1])
    project = isolated(Path(fixture["project"]))
    api_port, app_debug, dsh_debug = unused_port(), unused_port(), unused_port()
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    app_env = {**environment, "CLASSROOM_DATA_ROOT": fixture["data"],
               "CLASSROOM_API_PORT": str(api_port), "CLASSROOM_FRONTEND_PORT": str(unused_port()),
               "CLASSROOM_DESKTOP_DEBUG_PORT": str(app_debug)}
    dsh_env = {**environment, "DSH_HOME": str(harness_home)}
    processes = []
    logs = []
    evidence = {"fixture": str(project), "messages_sent_by_test": 0}
    app_browser = dsh_browser = None
    desktop = None
    try:
        for name, command, env, cwd in (
            ("dsh", [str(args.dsh_executable), f"--user-data-dir={electron_profile}",
                     f"--remote-debugging-port={dsh_debug}"], dsh_env, ROOT),
            ("classcue", [str(args.python), str(project / "scripts/desktop_app.py")], app_env, project),
        ):
            log = (output / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            if name == "classcue":
                desktop = process
        print("Started isolated native ClassCue and DSH", flush=True)
        await until(lambda: ipc(project, "status"), lambda state: state.get("ready"))
        client = PipeClient(harness_home / "classcue-bridge/connection.json")
        await until(lambda: client.call("status"), lambda value: value.get("client_connected"))
        async with async_playwright() as playwright:
            async def connect(port):
                return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            app_browser = await until(lambda: connect(app_debug), lambda value: bool(value))
            dsh_browser = await until(lambda: connect(dsh_debug), lambda value: bool(value))
            page = app_browser.contexts[0].pages[0]
            dsh = next(page for context in dsh_browser.contexts for page in context.pages
                       if page.url.startswith("dsh-app:"))
            assert not await dsh.get_by_role("button", name="开始设置", exact=True).is_visible(), (
                "Complete the isolated DSH profile's native first-run setup before acceptance"
            )
            await page.wait_for_function("typeof window.pywebview?.api?.describe === 'function'")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            dsh.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{api_port}"

            async def api(path):
                response = await page.request.get(base + path)
                assert response.ok, (response.status, path)
                return await response.json()

            status = await api("/api/library/status")
            await page.evaluate("""scope => localStorage.setItem(`classroom-onboarding:v1:${scope}`,
                JSON.stringify({dismissed:true}))""", status["library_scope"])
            await page.goto(base + "/study-materials")
            await page.get_by_role("heading", name="学习资料", exact=True).wait_for()
            await page.get_by_role("button", name="新建课程资料夹", exact=True).click()
            await page.get_by_label("课程名称", exact=True).fill("合成验收课程")
            await page.get_by_role("button", name="创建", exact=True).click()
            await page.get_by_role("button", name="导入资料", exact=True).first.wait_for()
            await page.get_by_label("选择学习资料", exact=True).set_input_files([
                {"name": "synthetic-cross-page.pdf", "mimeType": "application/pdf", "buffer": pdf_content()},
                {"name": "synthetic-scenario.md", "mimeType": "text/markdown",
                 "buffer": b"# Q3\n\nShared scenario R(A,B,C).\n\n(1) Find candidate keys.\n(2) Explain the normal form.\n"},
            ])
            await page.locator(".study-file-row").filter(has_text="synthetic-scenario.md").wait_for()
            await page.get_by_role("button", name="选题", exact=True).click()
            async def draw_page(index):
                canvas = page.locator(".study-page-canvas").nth(index)
                await canvas.scroll_into_view_if_needed()
                await canvas.locator("img").evaluate("image => image.decode()")
                bounds = await canvas.bounding_box()
                await page.mouse.move(bounds["x"] + bounds["width"] * .08, bounds["y"] + bounds["height"] * .1)
                await page.mouse.down()
                await page.mouse.move(bounds["x"] + bounds["width"] * .92, bounds["y"] + bounds["height"] * .55, steps=8)
                await page.mouse.up()

            await draw_page(0)
            await page.get_by_role("button", name="添加另一区域", exact=True).click()
            await draw_page(1)
            await page.get_by_label("题目标题", exact=True).fill("Synthetic whole question")
            await page.get_by_label("原题号", exact=True).fill("3 (1)(2)")
            assert await page.locator(".dsh-question-actions").count() == 0
            await page.get_by_role("button", name="保存题目", exact=True).click()
            await page.get_by_text("题目已保存", exact=True).wait_for()
            await page.get_by_role("button", name="阅读", exact=True).click()
            await page.get_by_role("button", name="DSH 桌面连接", exact=True).click()
            await page.get_by_text("自定义 DSH profile", exact=True).click()
            await page.get_by_label("插件连接文件", exact=True).fill(str(harness_home / "classcue-bridge/connection.json"))
            await page.get_by_role("button", name="连接", exact=True).click()
            root = project / "discussion-workspaces"
            root.mkdir()
            response = await page.request.put(base + "/api/dsh-bridge/workspace-root", data={
                "path": str(root), "instance_id": client.call("status")["instance_id"]})
            assert response.ok, await response.text()
            await page.get_by_role("button", name="关闭 DSH 连接", exact=True).click()
            await page.get_by_role("dialog", name="DSH 桌面连接", exact=True).wait_for(state="detached")
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()

            documents = await api("/api/study-documents")
            pdf = next(document for document in documents if document["format"] == "pdf")
            markdown = next(document for document in documents if document["format"] == "markdown")
            question = (await api(f"/api/study-questions?document_id={pdf['id']}"))[0]
            assert len(question["regions"]) == 2

            async def history(question_id):
                return await api(f"/api/dsh-bridge/questions/{question_id}/discussions")

            async def pending_request(question_id):
                response = await page.request.post(base + "/api/dsh-bridge/discussions",
                                                   data={"question_id": question_id})
                assert response.ok, (response.status, "pending question")
                return (await response.json())["request_id"]

            assert await history(question["id"]) == []
            request_id = await pending_request(question["id"])
            receipt = await until(lambda: api(f"/api/dsh-bridge/requests/{request_id}"),
                                  lambda value: value["status"] == "draft_ready")
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            for image in await dsh.locator('img[src^="blob:"]').all():
                await image.hover(trial=True)
            await dsh.locator('[contenteditable="true"]').hover(trial=True)
            await page.screenshot(path=str(output / "classcue-saved-pdf.png"))
            await dsh.screenshot(path=str(output / "dsh-two-crops.png"))
            assert not "\n".join(await dsh.locator('[contenteditable="true"]').all_text_contents()).strip()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            assert await history(question["id"]) == []
            assert (await api(f"/api/dsh-bridge/requests/{request_id}"))["session_id"] == receipt["session_id"]
            assert await dsh.locator('img[src^="blob:"]').count() == 2
            print("PDF saved through native reader; two crops in one unsent DSH draft; reopen is idempotent", flush=True)

            await page.get_by_role("button", name="编辑题目", exact=True).click()
            assert await page.locator(".dsh-question-actions").count() == 0
            await page.get_by_role("button", name="取消", exact=True).click()
            await page.locator(".study-file-open").filter(has_text="synthetic-scenario.md").click()
            await page.locator(".study-markdown-block").first.wait_for()
            await page.get_by_role("button", name="选题", exact=True).click()
            await page.locator(".study-block-check input").first.wait_for()
            for checkbox in await page.locator(".study-block-check input").all():
                await checkbox.check()
            await page.get_by_role("button", name="保存题目", exact=True).click()
            await page.get_by_text("题目已保存", exact=True).wait_for()
            await page.get_by_role("button", name="阅读", exact=True).click()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            md_question = (await api(f"/api/study-questions?document_id={markdown['id']}"))[0]
            assert await history(md_question["id"]) == []
            md_request = await pending_request(md_question["id"])
            md_receipt = await until(lambda: api(f"/api/dsh-bridge/requests/{md_request}"),
                                     lambda value: value["status"] == "draft_ready")
            assert md_receipt["session_id"] != receipt["session_id"]
            assert md_receipt["workspace_id"] != receipt["workspace_id"]
            assert Path(md_receipt["cwd"]).parent == Path(receipt["cwd"]).parent
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 0)
            text = "\n".join(await dsh.locator('[contenteditable="true"]').all_text_contents())
            assert "(1) Find candidate keys." in text and "(2) Explain the normal form." in text
            await dsh.locator('[contenteditable="true"]').hover(trial=True)
            await page.screenshot(path=str(output / "classcue-saved-markdown.png"))
            await dsh.screenshot(path=str(output / "dsh-whole-markdown.png"))
            await page.reload()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).wait_for()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            assert await history(md_question["id"]) == []
            await page.locator(".study-file-open").filter(has_text="synthetic-cross-page.pdf").click()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            for image in await dsh.locator('img[src^="blob:"]').all():
                await image.hover(trial=True)
            await dsh.locator('[contenteditable="true"]').hover(trial=True)
            assert (await api(f"/api/dsh-bridge/requests/{request_id}"))["session_id"] == receipt["session_id"]
            await dsh_browser.close()
            dsh_browser = None
            isolated_dsh = processes[0]
            if isolated_dsh.poll() is None:
                subprocess.run(["taskkill", "/PID", str(isolated_dsh.pid), "/T", "/F"], capture_output=True, check=True)
                isolated_dsh.wait(timeout=10)
            replacement = subprocess.Popen(
                [str(args.dsh_executable), f"--user-data-dir={electron_profile}",
                 f"--remote-debugging-port={dsh_debug}"],
                cwd=ROOT, env=dsh_env, stdout=logs[0], stderr=logs[0],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            processes.append(replacement)
            await until(lambda: client.call("status"), lambda value: value.get("client_connected"))
            dsh_browser = await until(lambda: connect(dsh_debug), lambda value: bool(value))
            dsh = next(page for context in dsh_browser.contexts for page in context.pages
                       if page.url.startswith("dsh-app:"))
            dsh.on("pageerror", lambda error: errors.append(str(error)))
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            restored = await api(f"/api/dsh-bridge/requests/{request_id}")
            assert restored["session_id"] == receipt["session_id"] and restored["status"] == "draft_ready"
            assert (await api(f"/api/dsh-bridge/requests/{md_request}"))["status"] == "draft_ready"
            await dsh.screenshot(path=str(output / "dsh-after-restart.png"))
            assert not errors, errors
            evidence.update(pdf_session=receipt["session_id"], markdown_session=md_receipt["session_id"],
                            pdf_regions=2, dsh_restart="same session and two restored images",
                            javascript_errors=errors, result="passed")
            print("Markdown subquestions stay together; reload and cross-question navigation restore original drafts", flush=True)
    finally:
        for browser in (app_browser, dsh_browser):
            if browser:
                await browser.close()
        if desktop and desktop.poll() is None:
            try:
                ipc(project, "exit")
                await until(lambda: desktop.poll(), lambda code: code is not None, timeout=20)
            except Exception:
                subprocess.run([str(args.python), str(project / "scripts/start_frontend.py"), "stop"],
                               cwd=project, env=app_env, capture_output=True, timeout=30)
        for process in reversed(processes):
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                process.wait(timeout=10)
        for log in logs:
            log.close()
        (output / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("PASS: native desktop acceptance; no model messages sent; isolated processes stopped", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--dsh-executable", type=Path, required=True)
    parser.add_argument("--harness-home", type=Path, required=True)
    parser.add_argument("--electron-profile", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
