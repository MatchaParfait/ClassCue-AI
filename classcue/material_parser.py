"""Local parsing only; no model requests or changes to imported originals."""

from __future__ import annotations

from io import BytesIO
import hashlib
from pathlib import Path
import re
from zipfile import ZipFile

from classcue.processing_control import ProcessingCancelled


FORMATS = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
           ".md": "markdown", ".markdown": "markdown", ".docx": "docx"}
MAX_PAGES = 500
MAX_IMAGE_PIXELS = 60_000_000


def checkpoint(should_cancel):
    if should_cancel():
        raise ProcessingCancelled("已停止资料整理，已完成内容保留")


def image_asset(data, output: Path):
    from PIL import Image, ImageOps
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("图片尺寸过大，请拆分或缩小图片")
        image = ImageOps.exif_transpose(image).convert("RGB")
        width, height = image.size
        image.save(output, "PNG")
    return width, height


def parse_document(repo, document_id, should_cancel=lambda: False):
    document = repo.document(document_id)
    if document["deleted"]:
        raise ValueError("文件已移除")
    source = repo.resource(document["resource"])
    with source.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != document["sha256"]:
            repo.document_state(document_id, "failed", "管理目录内原文件的完整性校验失败，请重新导入")
            raise ValueError("原文件完整性校验失败")
    if repo.blocks(document_id):
        return repo.blocks(document_id)
    directory = source.parent
    checkpoint(should_cancel)
    try:
        if document["format"] == "pdf":
            blocks = parse_pdf(source, directory, repo.root, should_cancel)
        elif document["format"] == "image":
            width, height = image_asset(source.read_bytes(), directory / "page.png")
            blocks = [{"type": "image", "page": 1, "text": "", "width": width, "height": height,
                       "image": (directory / "page.png").relative_to(repo.root).as_posix()}]
        elif document["format"] == "markdown":
            blocks = parse_markdown(source)
        else:
            blocks = parse_docx(source, directory, repo.root, should_cancel)
        checkpoint(should_cancel)
        repo.save_blocks(document_id, blocks or [{"type": "empty", "text": "", "issue": "文件没有可读内容"}])
    except ProcessingCancelled:
        raise
    except Exception as error:
        repo.document_state(document_id, "failed", "文件解析失败，请检查格式、加密或损坏情况")
        raise ValueError("文件解析失败，请检查格式、加密或损坏情况") from error
    return repo.blocks(document_id)


def parse_pdf(source, directory, root, should_cancel):
    import pdfplumber
    import pypdfium2 as pdfium
    blocks = []
    with pdfplumber.open(source) as pdf, pdfium.PdfDocument(source) as renderer:
        if len(pdf.pages) > MAX_PAGES:
            raise ValueError("PDF 超过 500 页，请按卷拆分")
        for index, page in enumerate(pdf.pages):
            checkpoint(should_cancel)
            block = {"type": "page", "page": index + 1, "text": "", "width": float(page.width),
                     "height": float(page.height), "words": []}
            try:
                rendered = renderer[index]
                try:
                    scale = min(2, 2200 / max(page.width, page.height))
                    bitmap = rendered.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        target = directory / f"page-{index + 1}.png"
                        image.save(target)
                        block["image"] = target.relative_to(root).as_posix()
                    finally:
                        bitmap.close()
                finally:
                    rendered.close()
            except Exception:
                block["issue"] = "此页图像无法渲染，请提供清晰页面或转换后的 PDF"
            try:
                block["text"] = page.extract_text(layout=True) or ""
                block["words"] = [{"text": w["text"], "bbox": [
                    w["x0"] / page.width, w["top"] / page.height, w["x1"] / page.width, w["bottom"] / page.height,
                ]} for w in page.extract_words()]
            except Exception:
                # A clear scanned page remains usable without OCR.
                if not block.get("image"):
                    block["issue"] = "此页文字和图像均不可读取"
            blocks.append(block)
            page.close()
    return blocks


def parse_markdown(source):
    from markdown_it import MarkdownIt
    text = source.read_text(encoding="utf-8-sig")
    if len(text) > 5_000_000:
        raise ValueError("Markdown 过长，请拆分文件")
    lines = text.splitlines(keepends=True)
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(text)
    # Keep outer list/table/fence ranges together so source Markdown remains renderable.
    ranges = []
    for token in tokens:
        if token.map and token.level == 0:
            start, end = token.map
            if not ranges or start >= ranges[-1][1]:
                ranges.append((start, end))
    blocks = []
    cursor = 0
    for start, end in ranges:
        if start > cursor and "".join(lines[cursor:start]).strip():
            blocks.append(_md_block(lines, cursor, start))
        blocks.extend(_markdown_question_blocks(lines, start, end))
        cursor = end
    if cursor < len(lines) and "".join(lines[cursor:]).strip():
        blocks.extend(_markdown_question_blocks(lines, cursor, len(lines)))
    return blocks


QUESTION_LINE = re.compile(r"^\s*(?:\d+[.)、]|[（(]\d+[）)])\s+")


def _markdown_question_blocks(lines, start, end):
    """Split a section containing numbered subquestions into source-addressable blocks."""
    question_starts = [index for index in range(start, end) if QUESTION_LINE.match(lines[index])]
    if len(question_starts) < 2:
        return [_md_block(lines, start, end)]
    blocks = []
    if question_starts[0] > start and "".join(lines[start:question_starts[0]]).strip():
        blocks.append(_md_block(lines, start, question_starts[0]))
    for index, question_start in enumerate(question_starts):
        question_end = question_starts[index + 1] if index + 1 < len(question_starts) else end
        blocks.append(_md_block(lines, question_start, question_end))
    return blocks


def _md_block(lines, start, end):
    from markdown_it import MarkdownIt
    text = "".join(lines[start:end])
    issue = ""
    for token in MarkdownIt().parse(text):
        for child in token.children or []:
            if child.type == "image":
                issue = "Markdown 引用了未导入的图片，请补充题图；不会自动读取本机路径或下载外链"
    return {"type": "markdown", "text": text, "line_start": start + 1, "line_end": end, "issue": issue}


def parse_docx(source, directory, root, should_cancel):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    with ZipFile(source) as archive:
        if sum(info.file_size for info in archive.infolist()) > 200_000_000:
            raise ValueError("Word 解压后过大，请拆分文档")
    document = Document(source)
    blocks = []
    image_number = 0
    w = lambda name: qn("w:" + name)
    for element in document.element.body:
        checkpoint(should_cancel)
        name = element.tag.rsplit("}", 1)[-1]
        if name == "sectPr":
            continue
        issue = ""
        images = []
        text = ""
        if name == "tbl":
            table = Table(element, document)
            rows = [[cell.text.replace("|", "\\|").replace("\n", " ") for cell in row.cells] for row in table.rows]
            if rows:
                text = "\n".join(["| " + " | ".join(rows[0]) + " |",
                                  "| " + " | ".join("---" for _ in rows[0]) + " |",
                                  *["| " + " | ".join(row) + " |" for row in rows[1:]]])
        else:
            # Preserve the OOXML text, tabs, breaks and simple run formatting in document order.
            chunks = []
            for run in element.iter(w("r")):
                raw = "".join(node.text or "" if node.tag == w("t") else
                              "\t" if node.tag == w("tab") else "\n" if node.tag in {w("br"), w("cr")} else ""
                              for node in run)
                properties = run.find(w("rPr"))
                if raw and properties is not None:
                    if properties.find(w("b")) is not None:
                        raw = "**" + raw + "**"
                    if properties.find(w("i")) is not None:
                        raw = "*" + raw + "*"
                chunks.append(raw)
            text = "".join(chunks)
            styles = element.findall(".//" + w("pStyle"))
            if styles and styles[0].get(w("val"), "").lower().startswith("heading"):
                text = "## " + text
            style_id = styles[0].get(w("val")) if styles else None
            paragraph_style = next((s for s in document.styles if s.style_id == style_id), None) if style_id else None
            if (element.findall(".//" + w("numPr"))
                    or paragraph_style is not None and paragraph_style.element.findall(".//" + w("numPr"))):
                issue = "Word 自动编号未可靠还原，请转换 PDF 或补图后核对原题号"
        for node in element.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "blip":
                rel_id = node.get(qn("r:embed"))
                part = document.part.related_parts.get(rel_id)
                if part:
                    image_number += 1
                    output = directory / f"embedded-{image_number}.png"
                    try:
                        width, height = image_asset(part.blob, output)
                        images.append({"image": output.relative_to(root).as_posix(), "width": width, "height": height})
                    except Exception:
                        issue = "Word 内嵌图片无法读取，请转换为 PDF 或补图"
                else:
                    issue = "Word 图片缺失或使用外部链接，请补图"
            if local in {"oMath", "oMathPara", "OLEObject", "pict", "altChunk", "anchor", "del", "ins",
                         "chart", "relIds", "txbxContent", "vertAlign", "gridSpan", "vMerge", "footnoteReference"}:
                issue = "Word 含公式、复杂对象、浮动布局或修订，无法可靠还原，请转换 PDF 或补图"
            if local == "graphicData" and not node.get("uri", "").endswith("/picture"):
                issue = "Word 含非图片绘图对象，请转换 PDF 或补图"
        if text.strip() or images or issue:
            blocks.append({"type": "docx", "text": text, "images": images, "issue": issue,
                           "block_number": len(blocks) + 1})
    seen = set()
    for section in document.sections:
        for area in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                     section.even_page_header, section.even_page_footer):
            if area.part.partname in seen:
                continue
            seen.add(area.part.partname)
            text = "\n".join(p.text for p in area.paragraphs).strip()
            if text or any(n.tag.rsplit("}", 1)[-1] in {"drawing", "pict", "tbl"} for n in area._element.iter()):
                blocks.append({"type": "docx", "text": text, "block_number": len(blocks) + 1,
                               "issue": "Word 页眉或页脚含内容，未自动混入题干，请转换 PDF 后核对"})
    return blocks
