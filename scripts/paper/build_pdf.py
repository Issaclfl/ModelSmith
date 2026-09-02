"""论文 PDF 编译：Markdown → Typst → PDF（CUMCM 排版模板内嵌）。

用法：
    python build_pdf.py 论文.md [-o 论文.pdf] [--typst-bin 路径]

管线：typst_export.md_to_typst（内嵌 CUMCM 模板：a4 / 2.5cm 边距 /
SimSun 12pt / 首行缩进 2em / 公式全局编号 (1) / SimHei 四级标题）
→ typst 编译为 PDF。

编译器查找顺序：
  1. --typst-bin 参数
  2. 环境变量 TYPST_BIN
  3. typst-py（pip install typst，纯 pip 安装，推荐）
  4. PATH 上的 typst 命令

字体：模板默认 SimSun（宋体正文）/ SimHei（黑体标题），Windows 系统自带；
Linux/macOS 需安装 Noto CJK（如 apt install fonts-noto-cjk）；若本目录
存在 fonts/ 子目录，会作为附加字体目录传给 typst。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from typst_export import md_to_typst  # noqa: E402

_FONT_DIRS = []
_here_fonts = Path(__file__).parent / "fonts"
if _here_fonts.is_dir():
    _FONT_DIRS.append(str(_here_fonts))


def _typst_py_available() -> bool:
    try:
        import typst  # noqa: F401
        return True
    except ImportError:
        return False


def compile_pdf(typ_path: Path, pdf_path: Path, typst_bin: str = "") -> str:
    """编译 .typ → .pdf。返回实际使用的编译器描述。"""
    bin_path = typst_bin or os.environ.get("TYPST_BIN", "")

    # 1/2. 显式指定的 typst 二进制
    if bin_path:
        cmd = [bin_path, "compile"]
        for d in _FONT_DIRS:
            cmd += ["--font-path", d]
        cmd += [str(typ_path), str(pdf_path)]
        subprocess.run(cmd, check=True)
        return f"typst CLI: {bin_path}"

    # 3. typst-py
    if _typst_py_available():
        import typst
        if _FONT_DIRS:
            typst.compile(str(typ_path), output=str(pdf_path),
                          font_paths=_FONT_DIRS)
        else:
            typst.compile(str(typ_path), output=str(pdf_path))
        return "typst-py"

    # 4. PATH 上的 typst
    which = shutil.which("typst")
    if which:
        cmd = [which, "compile"]
        for d in _FONT_DIRS:
            cmd += ["--font-path", d]
        cmd += [str(typ_path), str(pdf_path)]
        subprocess.run(cmd, check=True)
        return "typst CLI (PATH)"

    raise RuntimeError(
        "未找到 typst 编译器。任选其一：\n"
        "  pip install typst                     （推荐，typst-py）\n"
        "  或安装 typst CLI 并加入 PATH\n"
        "  或用 --typst-bin 指定 typst 可执行文件路径")


def main() -> None:
    ap = argparse.ArgumentParser(description="论文 PDF 编译（md → typ → pdf）")
    ap.add_argument("paper", help="论文 Markdown 文件")
    ap.add_argument("-o", dest="pdf_out", default="", help="输出 PDF 路径（默认与论文同目录同名）")
    ap.add_argument("--typst-bin", dest="typst_bin", default="",
                    help="typst 可执行文件路径（默认自动查找）")
    args = ap.parse_args()

    md_path = Path(args.paper)
    if not md_path.exists():
        print(f"论文不存在: {md_path}")
        sys.exit(2)

    md = md_path.read_text(encoding="utf-8")
    typ = md_to_typst(md)

    # .typ 与论文同目录（保证相对路径引用的图片可被 typst 解析）
    typ_path = md_path.with_suffix(".typ")
    typ_path.write_text(typ, encoding="utf-8")

    pdf_path = Path(args.pdf_out) if args.pdf_out else md_path.with_suffix(".pdf")
    compiler = compile_pdf(typ_path, pdf_path, args.typst_bin)

    size_kb = pdf_path.stat().st_size / 1024
    print(f"PDF: {pdf_path}（{size_kb:.0f} KB，编译器：{compiler}）")
    print(f"Typst 源: {typ_path}")


if __name__ == "__main__":
    main()
