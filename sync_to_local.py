# -*- coding: utf-8 -*-
"""把本仓同步到本机 skill 安装目录（安装器 + 测试循环工具）。

仓库是唯一真源：改完 skill 后运行本脚本，宿主 agent（ZCode/Claude Code）
读到的才是新版。会删除目标目录中本仓已不含的文件（含过期 __pycache__）。

用法：
    python sync_to_local.py             # 同步到所有已存在的安装目录
    python sync_to_local.py --dry-run   # 只看将变更的文件，不落盘
    python sync_to_local.py --target D:\\some\\skills   # 额外指定安装根目录
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "modelsmith"               # 须与 SKILL.md frontmatter 的 name 一致
INCLUDE = ("SKILL.md", "references", "examples", "scripts")
EXCLUDE_DIRS = {"__pycache__", ".git"}


def collect() -> dict[str, Path]:
    """收集本仓应分发的文件 -> {相对路径(posix): 绝对路径}。"""
    out: dict[str, Path] = {}
    for item in INCLUDE:
        p = ROOT / item
        if p.is_file():
            out[item] = p
        elif p.is_dir():
            for f in p.rglob("*"):
                rel = f.relative_to(ROOT)
                if f.is_file() and not (EXCLUDE_DIRS & set(rel.parts)):
                    out[str(rel).replace("\\", "/")] = f
    return out


def sync(target_root: Path, files: dict[str, Path], dry: bool) -> None:
    dst_root = target_root / NAME
    changed: list[str] = []
    for rel, src_path in sorted(files.items()):
        d = dst_root / rel
        if not d.exists() or not filecmp.cmp(src_path, d, shallow=False):
            changed.append(rel)
            if not dry:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, d)

    removed: list[str] = []
    if dst_root.exists():
        for f in sorted(dst_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            rel = str(f.relative_to(dst_root)).replace("\\", "/")
            if f.is_file() and rel not in files:
                removed.append(rel)
                if not dry:
                    f.unlink()
            elif f.is_dir() and not any(f.iterdir()):
                if not dry:
                    f.rmdir()

    tag = "[dry-run] " if dry else ""
    print(f"{tag}{dst_root}")
    for rel in changed:
        print(f"  + {rel}")
    for rel in removed:
        print(f"  - {rel}（目标多余，已删/将删）")
    if not changed and not removed:
        print("  （无差异，已是最新）")


def default_targets() -> list[Path]:
    home = Path.home()
    cands = [home / ".agents" / "skills", home / ".claude" / "skills"]
    return [c for c in cands if c.is_dir()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印将变更的文件")
    ap.add_argument("--target", action="append", default=[],
                    help="额外安装根目录（可多次）；默认自动探测 ~/.agents/skills 与 ~/.claude/skills")
    args = ap.parse_args()

    targets = default_targets() + [Path(t) for t in args.target]
    if not targets:
        sys.exit("未找到任何安装目录（~/.agents/skills、~/.claude/skills），"
                 "用 --target 指定一个")
    files = collect()
    print(f"本仓待分发文件 {len(files)} 个\n")
    for t in targets:
        if not t.is_dir():
            print(f"[跳过] {t} 不存在")
            continue
        sync(t, files, args.dry_run)
    if not args.dry_run:
        print("\n完成。新开会话即可用到新版技能。")


if __name__ == "__main__":
    main()
