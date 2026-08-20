"""知识文档统一解析层：md/txt/docx/pdf → 带标题的 Markdown 文本。"""

import argparse
import json
import os
import re
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

_SUPPORTED = {".md", ".txt", ".docx", ".pdf"}
_MAX_FILE_BYTES = 50 * 1024 * 1024


class DocumentExtractionError(RuntimeError):
    pass


def _clean_text(value):
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_plain(path):
    raw = open(path, "rb").read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentExtractionError("文本编码无法识别，请另存为 UTF-8 后重试")


def _paragraph_markdown(paragraph):
    text = _clean_text(paragraph.text)
    if not text:
        return ""
    style_name = str(getattr(paragraph.style, "name", "") or "")
    match = re.search(r"(?:Heading|标题)\s*([1-6])", style_name, re.I)
    if match:
        return f"{'#' * int(match.group(1))} {text}"
    if "List" in style_name or "列表" in style_name:
        return f"- {text}"
    return text


def _table_markdown(table):
    rows = []
    for row in table.rows:
        values = [
            _clean_text(cell.text).replace("|", "\\|").replace("\n", "<br>")
            for cell in row.cells
        ]
        if any(values):
            rows.append(values)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def _read_docx(path):
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocumentExtractionError(
            "缺少 python-docx，请重新安装项目依赖"
        ) from exc
    try:
        document = Document(path)
    except Exception as exc:
        raise DocumentExtractionError(f"Word 文档无法打开：{exc}") from exc
    blocks = []
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            rendered = _paragraph_markdown(item)
        elif isinstance(item, Table):
            rendered = _table_markdown(item)
        else:
            rendered = ""
        if rendered:
            blocks.append(rendered)
    text = "\n\n".join(blocks)
    if not _clean_text(text):
        raise DocumentExtractionError(
            "Word 文档没有可提取文字；图片、文本框或扫描内容暂不支持"
        )
    return text, {"pageCount": None}


def _read_pdf_ocr(path):
    """当 PDF 缺少字符文本层（如设计稿转曲、矢量画板、扫描件）时，使用 pypdfium2 渲染 + RapidOCR 自动识别。"""
    try:
        import pypdfium2 as pdfium
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise DocumentExtractionError(
            "PDF 没有可提取的文本层（可能是设计稿转曲或扫描件），且未安装 OCR 支持模块"
        ) from exc

    try:
        ocr = RapidOCR()
        with pdfium.PdfDocument(path) as pdf:
            page_count = len(pdf)
            pages = []
            for index, page in enumerate(pdf, start=1):
                bitmap = page.render(scale=1.5).to_numpy()
                result, _ = ocr(bitmap)
                if result:
                    page_lines = [line[1] for line in result if line and len(line) > 1 and line[1]]
                    page_text = _clean_text("\n".join(page_lines))
                    if page_text:
                        pages.append(f"## 第 {index} 页\n\n{page_text}")
        text = "\n\n".join(pages)
        if not _clean_text(text):
            raise DocumentExtractionError("PDF 中未识别到有效文字内容")
        return text, {"pageCount": page_count, "ocr": True}
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(f"PDF OCR 解析失败：{exc}") from exc


def _read_pdf(path):
    try:
        import pdfplumber
    except ImportError as exc:
        raise DocumentExtractionError(
            "缺少 pdfplumber，请重新安装项目依赖"
        ) from exc
    pages = []
    page_count = 0
    try:
        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            for index, page in enumerate(pdf.pages, start=1):
                text = _clean_text(
                    page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                )
                if text:
                    pages.append(f"## 第 {index} 页\n\n{text}")
    except Exception:
        pass

    text = "\n\n".join(pages)
    # 如果纯文本层提取字数过少（小于 15 个字），自动降级使用 RapidOCR
    if len(_clean_text(text)) < 15:
        try:
            return _read_pdf_ocr(path)
        except DocumentExtractionError:
            if not _clean_text(text):
                raise DocumentExtractionError(
                    "PDF 没有可提取的文本层（设计稿转曲或纯图片），且 OCR 未能识别出有效文字"
                )
    return text, {"pageCount": page_count}


def extract_document(path):
    path = os.path.abspath(str(path))
    if not os.path.isfile(path):
        raise DocumentExtractionError("文档不存在或已被移动")
    size = os.path.getsize(path)
    if size > _MAX_FILE_BYTES:
        raise DocumentExtractionError("文档超过 50 MB，暂不支持导入")
    extension = os.path.splitext(path)[1].lower()
    if extension not in _SUPPORTED:
        raise DocumentExtractionError(
            "暂只支持 Markdown、TXT、DOCX 和带文本层的 PDF"
        )
    if extension in {".md", ".txt"}:
        text, metadata = _read_plain(path), {"pageCount": None}
    elif extension == ".docx":
        text, metadata = _read_docx(path)
    else:
        text, metadata = _read_pdf(path)
    text = _clean_text(text)
    return {
        "text": text,
        "format": extension.lstrip("."),
        "charCount": len(text),
        "pageCount": metadata.get("pageCount"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--max-chars", type=int, default=0)
    args = parser.parse_args()
    try:
        result = extract_document(args.path)
        if args.max_chars > 0:
            result["text"] = result["text"][: args.max_chars]
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "message": str(exc)},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
