"""使用合成资料验收原生工作区树，不发送模型消息或读取用户凭据。"""

import argparse
import asyncio
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classcue.dsh_pipe import PipeClient
from tests.study_dsh_desktop import isolated, ipc, unused_port, until


async def main(args):
    home = isolated(args.harness_home)
    profile = isolated(args.electron_profile)
    run_id = str(time.time_ns())
    output = ROOT / ".runtime" / f"dsh-tree-acceptance-{run_id}"
    output.mkdir(parents=True)
    prepared = subprocess.run(
        [sys.executable, "tests/fixtures/desktop_application.py"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    )
    fixture = json.loads(prepared.stdout.strip().splitlines()[-1])
    project = isolated(Path(fixture["project"]))
    api_port, app_debug, dsh_debug = unused_port(), unused_port(), unused_port()
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    app_env = {**environment, "CLASSROOM_DATA_ROOT": fixture["data"], "CLASSROOM_API_PORT": str(api_port),
               "CLASSROOM_FRONTEND_PORT": str(unused_port()), "CLASSROOM_DESKTOP_DEBUG_PORT": str(app_debug)}
    dsh_env = {**environment, "DSH_HOME": str(home)}
    processes, logs = [], []
    browsers = []
    evidence = {"messages_sent_by_test": 0, "result": "failed"}
    try:
        for name, command, env, cwd in (
            ("dsh", [str(args.dsh_executable), f"--user-data-dir={profile}",
                     f"--remote-debugging-port={dsh_debug}", "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding"], dsh_env, ROOT),
            ("classcue", [sys.executable, str(project / "scripts/desktop_app.py")], app_env, project),
        ):
            log = (output / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log,
                                              creationflags=subprocess.CREATE_NO_WINDOW))
        await until(lambda: ipc(project, "status"), lambda value: value.get("ready"))
        client = PipeClient(home / "classcue-bridge/connection.json")
        status = await until(lambda: client.call("status"), lambda value: value.get("client_connected"))
        assert "workspace_tree" in status["capabilities"]
        async with async_playwright() as playwright:
            async def connect(port):
                return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            app_browser = await until(lambda: connect(app_debug), bool)
            dsh_browser = await until(lambda: connect(dsh_debug), bool)
            browsers.extend([app_browser, dsh_browser])
            page = app_browser.contexts[0].pages[0]
            dsh = next(p for c in dsh_browser.contexts for p in c.pages if p.url.startswith("dsh-app:"))
            # The separate login window stays untouched. Work only in the native renderer.
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            dsh.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{api_port}"

            async def api(method, path, **kwargs):
                response = await page.request.fetch(base + path, method=method, **kwargs)
                assert response.ok, (response.status, path, await response.text())
                return await response.json()

            async def capture(target, name):
                await target.bring_to_front()
                try:
                    raw = await target.screenshot(timeout=8000)
                    with Image.open(BytesIO(raw)) as image:
                        low, high = image.convert("L").getextrema()
                        if high - low < 10:
                            raise ValueError("Blank Electron screenshot")
                    (output / name).write_bytes(raw)
                except Exception as error:
                    evidence.setdefault("screenshot_errors", []).append(f"{name}: {type(error).__name__}")

            await api("POST", "/api/dsh-bridge/connection",
                      data={"descriptor_path": str(home / "classcue-bridge/connection.json")})
            root = output / "classcue-AI-DSH"
            root.mkdir()
            # The native picker itself is covered separately; this verifies its returned path's full flow.
            configured = await api("PUT", "/api/dsh-bridge/workspace-root",
                                   data={"path": str(root), "instance_id": status["instance_id"]})
            questions = []
            for name in ["合成数据库课程", "合成算法课程"]:
                course = await api("POST", "/api/study-courses", data={"name": name})
                for paper in ["春季试卷.md", "秋季试卷.md"]:
                    document = await api("POST", "/api/study-documents",
                                         params={"name": paper, "course_id": course["id"]},
                                         data=b"# Q1\n\nShared scenario (1) Draw. (2) Explain.\n",
                                         headers={"Content-Type": "application/octet-stream"})
                    blocks = await api("GET", f"/api/study-documents/{document['id']}/blocks")
                    for number in ["1", "2"]:
                        question = await api("POST", "/api/study-questions", data={
                            "document_id": document["id"], "number": number, "title": f"第{number}题",
                            "regions": [{"block_id": b["id"]} for b in blocks],
                        })
                        questions.append((course, document, question))
            receipts = []
            for course, document, question in questions:
                receipt = await api("POST", "/api/dsh-bridge/discussions", data={"question_id": question["id"]})
                receipt = await until(
                    lambda: api("GET", f"/api/dsh-bridge/requests/{receipt['request_id']}"),
                    lambda value: value.get("status") == "draft_ready" or bool(value.get("error")),
                )
                assert receipt["status"] == "draft_ready" and not receipt.get("error"), receipt
                assert Path(receipt["cwd"]).name == Path(document["name"]).stem
                assert Path(receipt["cwd"]).parent.name == course["name"]
                receipts.append(receipt)
            assert len({r["workspace_id"] for r in receipts}) == 4
            assert len({r["session_id"] for r in receipts}) == 8
            all_workspaces = client.call("workspaces")
            managed = [w for w in all_workspaces if Path(w["path"]).is_relative_to(root)]
            assert len(managed) == 7
            await dsh.get_by_role("button", name="视图选项", exact=True).click()
            await dsh.get_by_text("按工作区树", exact=True).click()
            for workspace in managed:
                row = dsh.locator(f'[data-row-key="workspace:{workspace["workspace_id"]}"]')
                await row.wait_for()
                depth = len(Path(workspace["path"]).relative_to(root).parts)
                indent = await row.evaluate("el => getComputedStyle(el).getPropertyValue('--dsh-workspace-indent').trim()")
                assert indent == f"{depth * 12}px", (workspace["title"], indent, depth)
            for workspace in all_workspaces:
                if workspace in managed:
                    continue
                row = dsh.locator(f'[data-row-key="workspace:{workspace["workspace_id"]}"]')
                if await row.count() and await row.get_attribute("aria-expanded") == "true":
                    await row.click()
            await dsh.mouse.move(1200, 100)
            await capture(dsh, "native-workspace-tree.png")
            library = await api("GET", "/api/library/status")
            await page.goto(base + "/")
            await page.evaluate("""scope => localStorage.setItem(`classroom-onboarding:v1:${scope}`,
                JSON.stringify({dismissed:true}))""", library["library_scope"])
            course, document, question = questions[0]
            await page.goto(base + f"/study-materials?course={course['id']}")
            await page.locator(".study-file-open").filter(has_text=document["name"]).click()
            await page.locator(".study-question-open").filter(has_text="第1题").click()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).wait_for()
            await page.get_by_role("button", name="DSH 桌面连接", exact=True).click()
            dialog = page.get_by_role("dialog", name="DSH 桌面连接", exact=True)
            await dialog.get_by_text(str(root), exact=True).wait_for()
            await dialog.get_by_role("button", name="关闭 DSH 连接", exact=True).hover(trial=True)
            await capture(page, "classcue-root-setting.png")
            await dialog.get_by_role("button", name="关闭 DSH 连接", exact=True).click()
            await page.get_by_role("button", name="在 DSH 中讨论", exact=True).click()
            await until(lambda: dsh.locator('[contenteditable="true"]').all_text_contents(),
                        lambda parts: "Shared scenario" in "\n".join(parts))
            again = await api("GET", f"/api/dsh-bridge/requests/{receipts[0]['request_id']}")
            assert again["session_id"] == receipts[0]["session_id"]
            assert await api("GET", f"/api/dsh-bridge/questions/{question['id']}/discussions") == []
            # Exercise multi-image transport in this same document node without using real files.
            image = BytesIO()
            Image.new("RGB", (80, 40), "green").save(image, "PNG")
            image_part = {"type": "image", "media_type": "image/png",
                          "data": base64.b64encode(image.getvalue()).decode("ascii")}
            direct = {
                "request_id": f"images-{run_id}", "library_id": f"acceptance-images-{run_id}",
                "course_id": "course", "course_name": "合成图题课程", "document_id": "pdf",
                "question_id": "whole-question", "content_version": 1, "layout": "workspace_tree",
                "root_id": f"image-root-{run_id}", "source": "跨页试卷.pdf", "title": "跨页题",
                "content": [{"type": "text", "text": "完整情景题（1）（2）"}, image_part, image_part],
            }
            client.call("configure_root", {"library_id": direct["library_id"],
                        "config_id": direct["root_id"], "path": str(root)})
            image_receipt = client.call("prepare", direct)
            await until(lambda: client.call("request", {"request_id": direct["request_id"]}),
                        lambda value: value.get("status") == "draft_ready")
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            for img in await dsh.locator('img[src^="blob:"]').all():
                await img.hover(trial=True)
            client.call("open", {"request_id": direct["request_id"]})
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            await capture(dsh, "native-two-images.png")
            await asyncio.sleep(1.2)
            await dsh_browser.close()
            browsers.remove(dsh_browser)
            if processes[0].poll() is None:
                subprocess.run(["taskkill", "/PID", str(processes[0].pid), "/T", "/F"],
                               capture_output=True, check=True)
                processes[0].wait(timeout=10)
            replacement = subprocess.Popen(
                [str(args.dsh_executable), f"--user-data-dir={profile}", f"--remote-debugging-port={dsh_debug}",
                 "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"],
                cwd=ROOT, env=dsh_env, stdout=logs[0], stderr=logs[0], creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(replacement)
            await until(lambda: client.call("status"), lambda value: value.get("client_connected"))
            dsh_browser = await until(lambda: connect(dsh_debug), bool)
            browsers.append(dsh_browser)
            dsh = next(p for c in dsh_browser.contexts for p in c.pages if p.url.startswith("dsh-app:"))
            client.call("open", {"request_id": direct["request_id"]})
            await until(lambda: dsh.locator('img[src^="blob:"]').count(), lambda count: count == 2)
            restored = client.call("request", {"request_id": direct["request_id"]})
            assert restored["session_id"] == image_receipt["session_id"]
            assert restored["workspace_id"] == image_receipt["workspace_id"]
            await capture(dsh, "native-after-restart.png")
            assert not errors, errors
            evidence.update(result="passed", root_binding=configured, registered_tree_nodes=7,
                            independent_sessions=8, native_tree_visible=True, node_indentation_correct=True,
                            app_reopen_same_session=True, two_image_draft_visible=True, restart_same_session=True,
                            visual_capture_complete=not evidence.get("screenshot_errors"),
                            image_session=image_receipt["session_id"], javascript_errors=errors)
            print("PASS: native registered hierarchy, per-document sessions, App settings and draft reopen", flush=True)
    except Exception:
        if browsers:
            for label, browser in zip(["classcue", "dsh"], browsers):
                try:
                    target = next(p for c in browser.contexts for p in c.pages
                                  if not p.url.endswith("/welcome.html"))
                    await target.screenshot(path=str(output / f"{label}-failure.png"), timeout=5000)
                except Exception:
                    pass
        raise
    finally:
        for browser in browsers:
            await browser.close()
        if len(processes) > 1 and processes[1].poll() is None:
            try:
                ipc(project, "exit")
                await until(lambda: processes[1].poll(), lambda code: code is not None, timeout=20)
            except Exception:
                subprocess.run([sys.executable, str(project / "scripts/start_frontend.py"), "stop"],
                               cwd=project, env=app_env, capture_output=True, timeout=30)
        for process in reversed(processes):
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                process.wait(timeout=10)
        for log in logs:
            log.close()
        (output / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Evidence: {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsh-executable", type=Path, required=True)
    parser.add_argument("--harness-home", type=Path, required=True)
    parser.add_argument("--electron-profile", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
