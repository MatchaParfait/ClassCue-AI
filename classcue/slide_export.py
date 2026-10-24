"""Export existing slide screenshots as chronological image-only PDF pages."""

from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader


def _timestamp(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds)) // 1000
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def write_slide_pdf(
    slides: Sequence[Mapping], slide_root: Path, output: BinaryIO, *, title: str,
) -> None:
    if not slides:
        raise ValueError("没有可导出的 PPT 图片")
    root = slide_root.resolve()
    document = Canvas(output, pageCompression=1)
    document.setTitle(title)
    document.setAuthor("ClassCue")
    for index, slide in enumerate(slides, 1):
        image_path = (root / str(slide["image_path"])).resolve()
        if not image_path.is_relative_to(root) or not image_path.is_file():
            raise ValueError("PPT 图片缺失或路径无效，请重新提取后再导出")
        image = ImageReader(str(image_path))
        width, height = image.getSize()
        page_width = 960
        image_height = page_width * height / width
        footer_height = 28
        document.setPageSize((page_width, image_height + footer_height))
        # JPEG resources are embedded directly; no screenshot resampling or re-encoding.
        document.drawImage(image, 0, footer_height, width=page_width, height=image_height)
        document.setFont("Helvetica", 10)
        document.setFillColorRGB(0.28, 0.28, 0.28)
        document.drawString(12, 10, f"{index} / {len(slides)}")
        document.drawCentredString(
            page_width / 2, 10,
            f"{_timestamp(slide['start_ms'])} - {_timestamp(slide['end_ms'])}",
        )
        document.drawRightString(page_width - 12, 10, f"@ {_timestamp(slide['representative_ms'])}")
        document.showPage()
    document.save()
