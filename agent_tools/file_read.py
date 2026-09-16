"""
工具名称: read_file
功能: 读取文件内容或列出文件夹内容，支持多种文档格式
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Union

# 尝试导入项目配置（如果可用）
try:
    from agent import PROJECT_ROOT, WORKSPACE_ROOT
except ImportError:
    # 降级方案：使用当前工作目录作为工作空间
    WORKSPACE_ROOT = Path.cwd()
    PROJECT_ROOT = Path.cwd()

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 辅助函数：检查路径是否在工作空间内（已注释限制）
# ------------------------------------------------------------
def _is_within_workspace(path: Path) -> bool:
    """检查路径是否在工作空间内（当前已无条件返回 True）"""
    return True  # 取消限制，允许访问任意路径

# ------------------------------------------------------------
# 目录列表格式化
# ------------------------------------------------------------
def _format_directory_listing(directory: Path) -> str:
    """格式化目录列表，返回结构化文本"""
    items = []
    try:
        for item in sorted(directory.iterdir()):
            try:
                stat = item.stat()
                is_dir = item.is_dir()
                size = stat.st_size if not is_dir else 0
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                item_type = "📁" if is_dir else "📄"
                items.append(f"{item_type} {item.name}  {size} bytes  {mtime}")
            except PermissionError:
                items.append(f"🔒 {item.name} (无权限访问)")
        if not items:
            return "目录为空"
        return "\n".join(items)
    except PermissionError:
        return "❌ 无权限访问该目录"
    except Exception as e:
        return f"❌ 列出目录失败: {str(e)}"

# ------------------------------------------------------------
# 文档读取专用函数
# ------------------------------------------------------------
def _read_docx(file_path: Path) -> str:
    """读取 .docx 文件内容"""
    try:
        from docx import Document
    except ImportError:
        return "❌ 缺少依赖库 python-docx,请执行: pip install python-docx"
    try:
        doc = Document(file_path)
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text)
        return "\n".join(full_text)
    except Exception as e:
        return f"❌ 读取 DOCX 文件失败: {str(e)}"

def _read_pdf(file_path: Path) -> str:
    """读取 .pdf 文件内容（提取文本）"""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return "❌ 缺少依赖库 pypdf 或 PyPDF2,请执行: pip install pypdf"
    try:
        reader = PdfReader(file_path)
        full_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
        return "\n".join(full_text) if full_text else "(PDF 中未提取到文本)"
    except Exception as e:
        return f"❌ 读取 PDF 文件失败: {str(e)}"

def _read_xlsx(file_path: Path) -> str:
    """读取 .xlsx 文件内容（转为文本表格）"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return "❌ 缺少依赖库 openpyxl，请执行: pip install openpyxl"
    try:
        wb = load_workbook(file_path, data_only=True)
        all_text = []
        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            all_text.append(f"=== 工作表: {sheet_name} ===")
            for row in sheet.iter_rows(values_only=True):
                row_text = "\t".join(str(cell) if cell is not None else "" for cell in row)
                if row_text.strip():
                    all_text.append(row_text)
        return "\n".join(all_text) if all_text else "（XLSX 文件为空）"
    except Exception as e:
        return f"❌ 读取 XLSX 文件失败: {str(e)}"

# ------------------------------------------------------------
# 主函数
# ------------------------------------------------------------
def read_file(file_path: str) -> str:
    """
    读取文件内容或列出目录内容。

    参数:
        file_path: 文件或目录的路径（相对路径或绝对路径）

    返回:
        - 如果是目录：列出目录下的文件和子目录
        - 如果是文件：根据扩展名选择读取方式
          * .txt, .md, .py, .json, .csv, .log → 纯文本读取
          * .docx → 提取段落文本
          * .pdf → 提取页面文字
          * .xlsx → 转为表格文本
    """
    # 1. 解析路径
    path = Path(file_path)
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path

    # 2. 安全检查（已取消限制）
    # if not _is_within_workspace(path): ...

    # 3. 检查是否存在
    if not path.exists():
        return f"❌ 错误：路径 '{path}' 不存在。"

    # 4. 处理目录
    if path.is_dir():
        return _format_directory_listing(path)

    # 5. 处理文件（根据扩展名）
    ext = path.suffix.lower()
    try:
        # 文本文件
        if ext in ['.txt', '.md', '.py', '.json', '.csv', '.log', '.ini', '.cfg', '.yaml', '.yml', '.toml']:
            max_size = 10 * 1024 * 1024  # 10MB
            if path.stat().st_size > max_size:
                return f"⚠️ 文件过大(超过 {max_size//1024//1024}MB),拒绝读取。"
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        # DOCX
        elif ext == '.docx':
            return _read_docx(path)
        # PDF
        elif ext == '.pdf':
            return _read_pdf(path)
        # XLSX
        elif ext == '.xlsx':
            return _read_xlsx(path)
        # 其他格式
        else:
            return f"❌ 不支持读取该文件格式（{ext}）。目前支持: .txt, .md, .py, .json, .csv, .log, .docx, .pdf, .xlsx"
    except UnicodeDecodeError:
        return "❌ 错误：文件不是纯文本（编码不是 UTF-8），且不属于已知文档格式，无法读取。"
    except Exception as e:
        logger.error(f"读取文件失败: {e}")
        return f"❌ 读取文件出错: {str(e)}"

# ------------------------------------------------------------
# Schema（工具说明书）
# ------------------------------------------------------------
read_file_schema = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "读取文件内容或列出目录内容。\n"
            "支持的文件类型：\n"
            "  - 纯文本：.txt, .md, .py, .json, .csv, .log, .ini, .yaml 等\n"
            "  - Word：.docx\n"
            "  - PDF：.pdf\n"
            "  - Excel：.xlsx\n"
            "如果是目录，返回文件列表（含大小和修改时间）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要读取的文件或目录路径（绝对或相对路径）"
                }
            },
            "required": ["file_path"]
        }
    }
}