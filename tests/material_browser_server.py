"""Isolated synthetic material library for the executable browser acceptance."""

import argparse
from io import BytesIO
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    sandbox = args.root.resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "data").mkdir(exist_ok=True)
    shutil.copyfile(project / "pyproject.toml", sandbox / "pyproject.toml")
    (sandbox / "data" / "ai_providers.json").write_text(
        json.dumps({"connections": [], "active_connection_id": ""}), encoding="utf-8")

    import classcue.paths
    classcue.paths.PROJECT_ROOT = sandbox
    import classcue.frontend_host as frontend
    mount = frontend.mount_frontend
    frontend.mount_frontend = lambda api, _: mount(api, project)
    from classcue.video_store import VideoStore
    from classcue.material_store import MaterialStore
    from classcue.material_parser import parse_document
    from api.main import create_app
    import uvicorn

    store = VideoStore(sandbox / "data" / "library" / "project.db")
    repo = MaterialStore(store)
    if not repo.collections():
        from reportlab.pdfgen import canvas
        collection = repo.save_collection("数据库原理 · 合成验收资料")
        topic = repo.save_category(collection["id"], name="数据库设计")
        kind = repo.save_category(collection["id"], name="绘制实体联系图（E-R 图）", parent_id=topic, aliases=["ER 图"])
        query_topic = repo.save_category(collection["id"], name="查询优化")
        query_type = repo.save_category(collection["id"], name="绘制优化后的查询树", parent_id=query_topic)
        graph_topic = repo.save_category(collection["id"], name="事务并发控制")
        graph_type = repo.save_category(collection["id"], name="绘制前驱图并判断可串行化", parent_id=graph_topic)
        for index in range(3):
            stream = BytesIO()
            pdf = canvas.Canvas(stream, pagesize=(600, 800))
            pdf.setFont("Helvetica", 14)
            pdf.drawString(40, 750, f"Synthetic exam {index + 1}")
            pdf.drawString(40, 690, "A library stores books, readers and loans.")
            pdf.drawString(40, 650, "1. Draw the entity relationship diagram.")
            pdf.drawString(40, 510, "2. Draw the optimized query tree for the following SQL.")
            pdf.setFont("Courier", 11)
            pdf.drawString(40, 480, "SELECT title FROM Books WHERE price > 20;")
            pdf.setFont("Helvetica", 14)
            pdf.drawString(40, 350, "3. Draw a precedence graph. Is this schedule serializable?")
            pdf.setFont("Courier", 11)
            pdf.drawString(40, 320, "r1(A), w2(A), r2(B), w1(B)")
            pdf.save()
            document = repo.register(collection["id"], f"模拟卷{index + 1}.pdf", stream.getvalue())
            block = parse_document(repo, document["id"])[0]
            context = repo.save_item(document["id"], {"title": "图书馆业务共享条件", "kind": "context",
                "sources": [{"block_id": block["id"], "bbox": [0.05, 0.1, 0.95, 0.16]}]})
            payloads = [
                {"title": f"图书借阅系统 E-R 图 · 卷{index + 1}", "number": "一（1）", "category_ids": [kind],
                 "parent_id": context["id"], "sources": [{"block_id": block["id"], "bbox": [0.05, 0.17, 0.95, 0.23]}]},
                {"title": f"选择与投影下推的查询树 · 卷{index + 1}", "number": "二", "category_ids": [query_type],
                 "sources": [{"block_id": block["id"], "bbox": [0.05, 0.34, 0.95, 0.42]}]},
                {"title": f"事务调度的冲突前驱图 · 卷{index + 1}", "number": "三", "category_ids": [graph_type],
                 "sources": [{"block_id": block["id"], "bbox": [0.05, 0.54, 0.95, 0.62]}]},
            ]
            with repo.store.connect() as db:
                for payload in payloads:
                    repo._save_item(db, document["id"], {**payload, "tags": ["绘图"]}, origin="automatic")
            repo.publish(document["id"], [], [{"block_id": block["id"], "state": "identified"}])
            repo.document_state(document["id"], "completed")
        markdown = repo.register(collection["id"], "公式与 SQL.md", (
            "# 关系代数练习\n\n给定关系 R(A,B) 和 S(B,C)。\n\n"
            "1. 写出关系代数表达式 $\\pi_{A,C}(R \\bowtie S)$ 的查询树。\n\n"
            "| 关系 | 元组数 |\n|---|---|\n| R | 100 |\n| S | 20 |\n\n"
            "```sql\nSELECT R.A,S.C FROM R JOIN S ON R.B=S.B;\n```\n").encode("utf-8"))
        blocks = parse_document(repo, markdown["id"])
        repo.save_item(markdown["id"], {"title": "关系代数与 SQL 查询树", "category_ids": [query_type],
            "tags": ["关系代数", "SQL"], "sources": [
                {"block_id": b["id"], "start": 0, "end": len(b["text"])} for b in blocks[1:]]})
        repo.publish(markdown["id"], [], [{"block_id": b["id"], "state": "non_question" if i == 0 else "identified"}
                                         for i, b in enumerate(blocks)])
        repo.document_state(markdown["id"], "completed")
        broken = repo.register(collection["id"], "待补图.md", "1. 根据附图画出 E-R 图。\n\n![附图](missing.png)".encode())
        parse_document(repo, broken["id"])
    uvicorn.run(create_app(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
