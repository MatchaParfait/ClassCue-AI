"""Source-preserving PDF, Markdown, and image parsing for manual study selection."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import hashlib
import math
import threading
from tempfile import NamedTemporaryFile

from classcue.study_materials import MAX_IMAGE_PIXELS


_PDF_RENDER_LOCK = threading.RLock()

def pdf_text_bounds(source: Path, page_index: int):
    """Return the union of all visible-page text, in the rendered page coordinates."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    with _PDF_RENDER_LOCK, pdfium.PdfDocument(source) as pdf:
        page = pdf[page_index]
        try:
            width, height = page.get_size()
            # Use PDFium's own transform so CropBox offsets and rotation match rendering.
            size_x, size_y = max(1, round(width * 10)), max(1, round(height * 10))
            transform = pdfium.PdfPosConv(page, (0, 0, size_x, size_y, 0))
            text = page.get_textpage()
            try:
                count = text.count_chars()
                if count > 200_000:
                    return {"status": "unavailable", "bbox": None}
                boxes = []
                for index in range(count):
                    code = raw.FPDFText_GetUnicode(text, index)
                    if not code or chr(code).isspace():
                        continue
                    left, bottom, right, top = text.get_charbox(index)
                    if not all(math.isfinite(v) for v in (left, bottom, right, top)):
                        continue
                    corners = [transform.to_bitmap(x, y) for x in (left, right) for y in (bottom, top)]
                    x0 = max(0, min(point[0] for point in corners) / size_x)
                    x1 = min(1, max(point[0] for point in corners) / size_x)
                    y0 = max(0, min(point[1] for point in corners) / size_y)
                    y1 = min(1, max(point[1] for point in corners) / size_y)
                    if x0 < x1 and y0 < y1:
                        boxes.append((x0, y0, x1, y1))
                if not boxes:
                    return {"status": "no_text", "bbox": None}
                # Do not classify columns or discard headers: use the maximum extent.
                return {"status": "ready", "bbox": [
                    min(box[0] for box in boxes), min(box[1] for box in boxes),
                    max(box[2] for box in boxes), max(box[3] for box in boxes),
                ]}
            finally:
                text.close()
        finally:
            page.close()


def parse_document_bytes(format_name, data, directory: Path, root: Path, document_id: str):
    if format_name == "pdf":
        blocks = _pdf_blocks(data, document_id)
        return blocks, len(blocks)
    if format_name == "markdown":
        blocks = _markdown_blocks(data, document_id)
        return blocks, 0
    if format_name == "image":
        from PIL import Image, ImageOps

        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("图片尺寸过大，请缩小后导入")
            image = ImageOps.exif_transpose(source).convert("RGB")
            width, height = image.size
            relative = (directory / "page.png").relative_to(root).as_posix()
            image.save(root / relative, "PNG")
        return [{"id": _block_id(document_id, 0), "kind": "image", "page": 1,
                 "image": relative, "width": width, "height": height}], 1
    raise ValueError("不支持的资料格式")


def _pdf_blocks(data, document_id):
    import pypdfium2 as pdfium

    try:
        with _PDF_RENDER_LOCK, pdfium.PdfDocument(data) as pdf:
            if len(pdf) == 0:
                raise ValueError("PDF 没有页面")
            if len(pdf) > 500:
                raise ValueError("PDF 超过 500 页，请拆分后导入")
            blocks = []
            for ordinal in range(len(pdf)):
                page = pdf[ordinal]
                try:
                    width, height = page.get_size()
                    blocks.append({
                        "id": _block_id(document_id, ordinal),
                        "kind": "pdf_page",
                        "page": ordinal + 1,
                        "width": float(width),
                        "height": float(height),
                    })
                finally:
                    page.close()
            return blocks
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF 无法读取，请检查文件是否损坏或受密码保护") from error


def render_pdf_page(source: Path, page_index: int, target: Path):
    import pypdfium2 as pdfium

    target.parent.mkdir(parents=True, exist_ok=True)
    # PDFium is process-global; serialize rendering and atomically publish cache files.
    with _PDF_RENDER_LOCK:
        if target.is_file():
            return target
        temporary = None
        try:
            with pdfium.PdfDocument(source) as pdf:
                if page_index < 0 or page_index >= len(pdf):
                    raise ValueError("PDF 页码超出范围")
                page = pdf[page_index]
                try:
                    width, height = page.get_size()
                    scale = min(3.0, 2400 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        with NamedTemporaryFile(dir=target.parent, suffix=".png", delete=False) as file:
                            temporary = Path(file.name)
                        bitmap.to_pil().save(temporary, "PNG")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            temporary.replace(target)
        except Exception as error:
            raise ValueError("PDF 页面无法渲染，请检查文件是否损坏") from error
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return target


def _markdown_blocks(data, document_id):
    from markdown_it import MarkdownIt

    if len(data) > 5_000_000:
        raise ValueError("Markdown 超过 5 MB，请拆分后导入")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Markdown 必须使用 UTF-8 编码") from error
    if not text.strip():
        raise ValueError("Markdown 没有可读内容")
    lines = text.splitlines(keepends=True)
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(text)
    ranges = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.level != 0 or token.map is None:
            index += 1
            continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            end_index = index + 1
            item_ranges = []
            while end_index < len(tokens):
                candidate = tokens[end_index]
                if candidate.type in {"bullet_list_close", "ordered_list_close"} and candidate.level == 0:
                    break
                if candidate.type == "list_item_open" and candidate.level == 1 and candidate.map:
                    item_ranges.append(tuple(candidate.map))
                end_index += 1
            ranges.extend(item_ranges or [tuple(token.map)])
            index = end_index + 1
            continue
        if token.type in {"heading_open", "paragraph_open", "table_open", "blockquote_open", "fence", "code_block", "hr"}:
            ranges.append(tuple(token.map))
        index += 1

    unique_ranges = []
    last_end = 0
    for start, end in ranges:
        start = max(start, last_end)
        if end <= start:
            continue
        chunk = "".join(lines[start:end])
        if chunk.strip():
            unique_ranges.append((start, end, chunk))
            last_end = end
    if not unique_ranges and text.strip():
        unique_ranges.append((0, len(lines), text))

    blocks = []
    for ordinal, (start, end, chunk) in enumerate(unique_ranges):
        image_tokens = [child for token in MarkdownIt("commonmark", {"html": False}).parse(chunk)
                        if token.type == "inline" for child in (token.children or []) if child.type == "image"]
        blocks.append({
            "id": _block_id(document_id, ordinal),
            "kind": "markdown",
            "text": chunk,
            "line_start": start + 1,
            "line_end": end,
            "has_missing_images": bool(image_tokens),
            "image_labels": [token.content or "未命名图片" for token in image_tokens],
        })
    return blocks


def _block_id(document_id, ordinal):
    return hashlib.sha256(f"{document_id}:{ordinal}".encode("ascii")).hexdigest()[:32]
