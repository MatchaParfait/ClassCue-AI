"""Serve the study reader against an isolated synthetic library."""

import argparse
from io import BytesIO
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18880)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    shutil.copyfile(project / "pyproject.toml", root / "pyproject.toml")
    (root / "data" / "ai_providers.json").write_text('{"connections":[],"active_connection_id":""}', encoding="utf-8")
    fixtures = root / "fixtures"
    fixtures.mkdir(exist_ok=True)
    from reportlab.pdfgen import canvas
    from PIL import Image, ImageDraw
    pdf = canvas.Canvas(str(fixtures / "synthetic-paper.pdf"), pagesize=(600, 800))
    for index in range(14):
        pdf.setFont("Helvetica-Bold", 20)
        pdf.drawString(40, 744, f"DATABASE SYSTEMS / PRACTICE {index + 1:02d}")
        pdf.setFont("Helvetica", 13)
        pdf.drawString(40, 690, "3. A library manages books, readers and borrowing records.")
        pdf.drawString(40, 663, "(1) Draw an entity relationship diagram.")
        pdf.drawString(40, 636, "(2) Convert the diagram into relational schemas.")
        pdf.rect(50, 430, 145, 90)
        pdf.rect(365, 430, 145, 90)
        pdf.drawString(91, 472, "Readers")
        pdf.drawString(410, 472, "Books")
        pdf.line(195, 475, 365, 475)
        pdf.drawString(240, 490, "Borrows")
        pdf.drawString(40, 350, "Use all the conditions and both subquestions in one exercise.")
        pdf.drawString(40, 60, f"Synthetic examination, page {index + 1} / 14")
        pdf.showPage()
    pdf.save()
    image = Image.new("RGB", (1000, 750), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 60), "Synthetic image: draw the graph and explain the result.", fill="black")
    draw.rectangle((100, 180, 430, 450), outline="black", width=4)
    image.save(fixtures / "synthetic-photo.png")
    (fixtures / "synthetic-notes.md").write_text(
        "# 数据库原理 · 练习\n\n## 第三题：关系模式\n\n给定关系 R(A,B) 与 S(B,C)。\n\n"
        "1. （1）写出查询表达式。\n   （2）画出查询树并说明下推步骤。\n2. 分析连接顺序。\n\n"
        "$$\n\\pi_{A,C}(R \\bowtie S)\n$$\n\n| 关系 | 元组数 |\n|---|---|\n| R | 100 |\n| S | 20 |\n\n"
        "```sql\nSELECT R.A, S.C FROM R JOIN S ON R.B=S.B;\n```\n\n![附图](https://example.invalid/missing.png)\n",
        encoding="utf-8",
    )
    (fixtures / "broken.pdf").write_bytes(b"not a PDF")

    import classcue.paths
    classcue.paths.PROJECT_ROOT = root
    import classcue.frontend_host as frontend
    mount = frontend.mount_frontend
    frontend.mount_frontend = lambda api, _: mount(api, project)
    from classcue.video_store import VideoStore
    from api.main import create_app
    import uvicorn
    store = VideoStore(root / "data" / "library" / "project.db")
    app = create_app(store)

    @app.middleware("http")
    async def qa_identity(request, call_next):
        if request.url.path == "/api/study-qa":
            from fastapi.responses import JSONResponse
            return JSONResponse({"synthetic": True})
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
