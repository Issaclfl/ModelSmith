# -*- coding: utf-8 -*-
"""求解沙箱：安全扫描 + 隔离执行 + 指标校验一体（agent 必经的执行入口）。

为什么必须用它而不用裸 shell 跑求解脚本（全部来自实战事故）：
- 安全 AST 扫描：拦 eval/exec/os.system/dunder 逃逸链/危险写文件
- 隔离临时目录执行：脚本与数据拷贝进去、图片与 metrics.json 拷出来，
  脚本写坏文件也污染不了工作区
- 超时控制：按算法动态超时（--algo），超时杀进程树
- "假装成功"检测：returncode=0 但无产出/stdout 含错误/SELFTEST FAIL ≠ 成功
- metrics.json 校验：`_expected_ranges` 逐项核对 + 强弱零键 + 误差/比率合理性
- 种子注入：random/numpy 种子统一注入（可复现，配合 run_stability.py）

用法：
    python scripts/solve/sandbox_run.py 求解脚本.py
    python scripts/solve/sandbox_run.py 脚本.py --algo 蒙特卡洛仿真 --seed 42
    python scripts/solve/sandbox_run.py --selftest

退出码：0=通过且无警告；1=执行/校验问题（含 metrics 超范围/零键告警）；2=安全拦截。
脚本内不要再自己设 random/numpy 种子——沙箱头部注入会覆盖你的设定。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ══════════════════════════════════════════════════════════
# 安全扫描（移植自 mathagent agents/builder.validate_code，实战口径）
# ══════════════════════════════════════════════════════════

def validate_code(code: str) -> tuple[bool, str]:
    """验证 Python 代码语法和安全性。"""
    if not code.strip():
        return False, "代码为空"

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误(第{e.lineno}行): {e.msg}"

    DANGEROUS_IMPORTS = {"subprocess", "shutil", "socket", "importlib"}
    DANGEROUS_CALLS = {
        "eval", "exec", "__import__",
        "import_module", "breakpoint",
    }
    DANGEROUS_ATTRS = {
        "system", "popen", "spawn",
        # os 写操作/删除（mkdir/makedirs 放行：生成代码常用其创建输出目录）
        "remove", "unlink", "rmdir", "chmod", "chown",
        "symlink", "link",
        # dunder 逃逸链：() -> __class__ -> __bases__ -> __subclasses__ -> Popen
        "__class__", "__bases__", "__subclasses__", "__mro__",
        "__globals__", "__builtins__", "__getattribute__", "__getattr__",
    }
    WRITE_MODES = {"w", "a", "x", "wb", "ab", "xb", "w+", "a+", "x+"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            return False, "禁止访问变量: __builtins__"

        # open()：允许写结果文件（metrics.json/csv/txt），禁止写代码/系统路径
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = "r"
            fname = ""
            is_const = node.args and isinstance(node.args[0], ast.Constant)
            if is_const:
                fname = str(node.args[0].value)
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            if mode in WRITE_MODES:
                if not is_const:
                    pass  # 变量文件名：放行（沙箱内隔离执行）
                elif (
                    fname.endswith((".json", ".csv", ".txt", ".xlsx"))
                    and "/" not in fname and "\\" not in fname and ":" not in fname
                    and not fname.startswith(".")
                ):
                    pass  # 结果类文件：允许
                else:
                    return False, f"禁止写非结果文件 (mode='{mode}', file='{fname}')"

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in DANGEROUS_IMPORTS:
                    return False, f"禁止导入危险模块: {alias.name}"

        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in DANGEROUS_IMPORTS:
                return False, f"禁止导入危险模块: {node.module}"

        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in DANGEROUS_CALLS:
                return False, f"禁止调用危险函数: {func_name}()"

        if isinstance(node, ast.Attribute):
            if node.attr in DANGEROUS_ATTRS:
                return False, f"禁止访问危险属性: .{node.attr}"

    return True, ""


def check_dependencies(code: str) -> list[str]:
    """检查代码依赖的库是否已安装（仅警告，不拦截）。"""
    missing = []
    try:
        tree = ast.parse(code)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        for imp in imports:
            try:
                spec = importlib.util.find_spec(imp)
                if spec is None:
                    missing.append(imp)
            except (ImportError, AttributeError, ModuleNotFoundError, ValueError):
                missing.append(imp)
    except SyntaxError:
        pass
    return missing


def sanitize_code(code: str) -> str:
    """入口清洗控制字符（LLM 生成代码偶发混入 NUL/裸回车）。"""
    return code.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def has_real_computation(code: str) -> bool:
    """检测代码是否包含真正的数值计算（而非 print+赋值的空跑）。"""
    indicators = [
        "for ", "while ", "scipy", "optimize", "minimize",
        "np.", "numpy", "math.", "sqrt", "sin", "cos",
    ]
    has_loop_or_optimize = any(ind in code for ind in indicators[:6])
    has_function = (
        re.search(r"def\s+\w+.*?:\s*\n(?:.*\n)*?.*return\s+", code) is not None
    )
    return has_loop_or_optimize or has_function


def has_selftest(code: str) -> bool:
    """AST 静态检查：是否定义 _selftest() 自检函数（跑通但结果错的第一道防线）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_selftest":
                return True
    return False


# ══════════════════════════════════════════════════════════
# 数据路径归一化（移植）：LLM 偶发硬编码绝对路径/任务目录段
# ══════════════════════════════════════════════════════════

_PATH_NORM_RE = re.compile(
    r"""(['"])                     # 捕获引号（单或双）
        (?:[A-Za-z]:[\\/]|/)       # 盘符+斜杠 或 Unix 根斜杠（绝对路径标志）
        .*                         # 中间任意路径（贪婪，锚定最后一个 data/）
        [\\/]data[\\/]             # 关键锚点：data/ 目录
        ([^'"\\]+)                 # 捕获文件名（不含路径分隔符和引号）
        \1                         # 匹配开引号
    """,
    re.VERBOSE,
)
_TASK_SEG_RE = re.compile(
    r"""(['"])(data[\\/])          # 引号 + data/
        (?:task_[^'"\\/]+[\\/])    # task_xxx/ 段（可多个）
        ([^'"\\]+)\1               # 文件名 + 匹配开引号
    """,
    re.VERBOSE,
)
_JOIN_TASK_SEG_RE = re.compile(
    r"""(os\.path\.join\(|Path\()        # join/Path 开头
        (["']data["']\s*,\s*)            # 'data', 参数
        (?:["']task_[^"']+["']\s*,\s*)+  # 'task_xxx', 参数（一层或多层）
        (["'][^"']+["'])                 # 文件名参数
    """,
    re.VERBOSE,
)
_PATH_OP_TASK_SEG_RE = re.compile(
    r"""(Path\(["']data["']\)\s*/\s*)    # Path('data') / 前缀
        (?:["']task_[^"']+["']\s*/\s*)+  # 'task_xxx' / 段（一层或多层）
        (["'][^"']+["'])                 # 文件名段
    """,
    re.VERBOSE,
)


def normalize_data_paths(code: str) -> str:
    """把代码中的绝对数据路径归一化为相对路径 data/文件名。"""
    code = _PATH_NORM_RE.sub(r"\1data/\2\1", code)
    code = _TASK_SEG_RE.sub(r"\1\2\3\1", code)
    code = _JOIN_TASK_SEG_RE.sub(r"\1\2\3", code)
    code = _PATH_OP_TASK_SEG_RE.sub(r"\1\2", code)
    return code


# ══════════════════════════════════════════════════════════
# 指标提取与校验（移植自 mathagent agents/solver.py）
# ══════════════════════════════════════════════════════════

_KEYWORD_RE = re.compile(
    r"(特征值|方差贡献|累计|得分|预测|误差|MAE|RMSE|成本|score|accuracy|"
    r"precision|recall|f1|objective|最优|最佳|总成本|满足率|覆盖率|"
    r"特征重要|importance|系数|coefficient|p-value|显著|置信区间|"
    r"站点|区域|迭代|generation|fitness|适应度)",
    re.IGNORECASE,
)
_NUMBER_PAIR_RE = re.compile(
    r"([\w\u4e00-\u9fff()/]+)\s*[:=]\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)"
)


def extract_metrics(stdout: str) -> dict:
    """从 stdout 提取结构化指标：表格行 / 关键词行 / 数值对。"""
    metrics: dict = {"tables": [], "key_lines": [], "numbers": {}}
    if not stdout:
        return metrics
    for line in stdout.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "|" in stripped and stripped.count("|") >= 3:
            metrics["tables"].append(stripped)
            continue
        if _KEYWORD_RE.search(stripped):
            metrics["key_lines"].append(stripped[:300])
            for label, value in _NUMBER_PAIR_RE.findall(stripped):
                try:
                    num = float(value)
                    metrics["numbers"].setdefault(label, []).append(num)
                except ValueError:
                    pass
    metrics["tables"] = metrics["tables"][:20]
    metrics["key_lines"] = metrics["key_lines"][:30]
    for k in metrics["numbers"]:
        metrics["numbers"][k] = metrics["numbers"][k][:5]
    return metrics


def check_expected_ranges(metrics_json: dict) -> dict | None:
    """声明式范围校验：对照 _expected_ranges 逐项核对实际值。"""
    ranges = metrics_json.get("_expected_ranges")
    if not isinstance(ranges, dict) or not ranges:
        return None
    violations: list[str] = []
    for key, rng in ranges.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            continue
        try:
            lo, hi = float(rng[0]), float(rng[1])
        except (TypeError, ValueError):
            continue
        val = metrics_json.get(key)
        vals = val if isinstance(val, list) else [val]
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if v < lo or v > hi:
                violations.append(f"{key}={v} 超出声明范围 [{lo}, {hi}]")
    if not violations:
        return None
    return {
        "problem": "out_of_expected_range",
        "detail": "；".join(violations[:5]),
        "hint": (
            "实际值超出代码自声明的物理合理范围，常见原因：\n"
            "① 公式/判定条件写反（符号错误）② 单位未换算（km/m、cm/m）\n"
            "③ 迭代发散或初值不当 ④ 若确认实际值正确，则是声明范围不合理，"
            "请修正 _expected_ranges 使其宽严适当"
        ),
    }


def check_result_sanity(metrics: dict, stdout: str) -> dict | None:
    """物理/逻辑合理性检查（能跑但结果错的情况，报错捕获不到）。"""
    problems: list[dict] = []
    numbers = metrics.get("numbers", {})

    # 1. 误差/分数类指标不应为负
    for key, values in numbers.items():
        for v in values:
            if key in ("MAE", "RMSE", "accuracy", "precision", "recall", "f1",
                       "覆盖率", "满足率", "得分", "score") and v < 0:
                problems.append({
                    "problem": "negative",
                    "detail": f"{key}={v} 为负值，不合理",
                    "hint": "检查目标函数符号（最小化 vs 最大化）或误差计算公式",
                })

    # 2. 相对误差/百分比过大（>50%）视为拟合失败
    for key, values in numbers.items():
        if any(k in key for k in ("误差", "error", "err")):
            for v in values:
                if v > 50:
                    problems.append({
                        "problem": "large_error",
                        "detail": f"{key}={v} 误差过大（>50%），疑似拟合失败",
                        "hint": "检查模型公式、数据预处理、参数范围是否合理",
                    })

    # 3. 概率/比率类指标应落在 [0,100]
    for key, values in numbers.items():
        if any(k in key for k in ("概率", "率", "ratio", "probability", "满足率",
                                  "覆盖率", "准确率")):
            for v in values:
                if v < 0 or v > 100:
                    problems.append({
                        "problem": "out_of_range",
                        "detail": f"{key}={v} 超出合理范围[0,100]",
                        "hint": "检查公式量纲和归一化处理",
                    })

    # 4. 全零/极小结果检测：强零键任意为 0 即告警；弱键全 0 才告警
    STRONG_ZERO = ("时长", "遮蔽", "覆盖", "duration", "shadow", "obscur",
                   "coverage", "interference")
    WEAK_ZERO = ("距离", "区间", "路径", "时间", "distance", "interval",
                 "path", "time", "收益", "利润", "产量", "吞吐", "服务", "length")

    strong_zero_keys = [
        k for k, values in numbers.items()
        if any(t in k for t in STRONG_ZERO)
        and any(abs(v) < 1e-9 for v in values)
    ]
    if strong_zero_keys:
        zero_names = "、".join(strong_zero_keys[:3])
        problems.append({
            "problem": "zero_value",
            "detail": f"{zero_names} 为 0——疑似几何判定/物理条件写反或坐标系错误，结果无意义",
            "hint": (
                f"【{zero_names} 全为 0 的常见原因】\n"
                "① 判定条件写反：如距离 < threshold 写成 > threshold\n"
                "② 坐标系不一致：如 ENU 局部坐标与地心坐标混用\n"
                "③ 单位未换算：如 km 和 m 混用导致距离计算偏差 1000 倍\n"
                "④ 数据读取失败：静默使用了全 0 默认值\n"
                "请先在 stdout 中查看中间结果（坐标、距离、判定），定位具体哪步出错"
            ),
        })
    else:
        weak_keys = [k for k in numbers if any(t in k for t in WEAK_ZERO)]
        if weak_keys and all(
            all(abs(v) < 1e-9 for v in numbers[k]) for k in weak_keys
        ):
            zero_names = "、".join(weak_keys[:3])
            problems.append({
                "problem": "zero_value",
                "detail": f"{zero_names} 等指标全部为 0——疑似几何判定/物理条件写反或坐标系错误，结果无意义",
                "hint": (
                    "所有物理量指标全为 0，代码可能没有做有效计算。\n"
                    "请检查：① 判定函数是否正确 ② 数据是否读取成功 ③ 计算是否被条件分支跳过"
                ),
            })

    if problems:
        all_details = "；".join(pr["detail"] for pr in problems[:5])
        return {"problem": problems[0]["problem"], "detail": all_details,
                "hint": problems[0]["hint"]}
    return None


# 随机类算法名（子串匹配）——这些算法必须再过 run_stability.py
STOCHASTIC_ALGOS = (
    "遗传算法", "粒子群优化", "模拟退火", "神经网络",
    "随机森林", "聚类分析", "蒙特卡洛仿真",
)
# 按算法动态超时（秒）：仿真/微分方程类计算量大，默认不够
TIMEOUT_BY_ALGO = {
    "常微分方程": 120, "微分方程": 120, "仿真": 120, "递推": 120,
    "蒙特卡洛": 90, "遗传": 90, "粒子群": 90, "模拟退火": 90, "神经": 120,
}
ERROR_PATTERNS = [
    "文件不存在", "文件未找到", "找不到文件", "No such file",
    "FileNotFoundError", "文件夹不存在", "目录不存在",
    "无法读取", "读取失败", "数据为空", "没有数据",
]


def resolve_timeout(algorithm: str | None, override: int | None) -> int:
    if override is not None:
        return int(override)
    if algorithm:
        for key, t in TIMEOUT_BY_ALGO.items():
            if key in algorithm:
                return t
    return 60


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉进程树（Windows 用 taskkill /T，POSIX 直接 kill）。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.kill()


def _copy_data_context(cwd: Path, tmp_dir: Path) -> list[str]:
    """把工作区的数据文件拷进沙箱 tmp/data/（保持相对路径可读）。"""
    copied: list[str] = []
    candidates: list[Path] = []
    data_root = cwd / "data"
    if data_root.is_dir():
        candidates.extend(p for p in data_root.rglob("*") if p.is_file())
    candidates.extend(p for p in cwd.glob("*") if p.is_file()
                      and p.suffix.lower() in (".csv", ".xlsx", ".txt"))
    for src in candidates:
        try:
            rel = src.relative_to(cwd)
        except ValueError:
            continue
        dest = tmp_dir / "data" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(str(rel).replace("\\", "/"))
        # 常用文件名别名：代码里常猜 data.xlsx/数据.csv
        if dest.suffix.lower() in (".csv", ".xlsx", ".txt"):
            for alias in ("data", "数据"):
                alias_path = dest.with_name(alias + dest.suffix)
                if not alias_path.exists():
                    shutil.copy2(dest, alias_path)
    return copied


_HEADER = (
    "# -*- coding: utf-8 -*-\n"
    "import sys, io\n"
    "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', "
    "errors='replace', write_through=True)\n"
    "try:\n"
    "    import matplotlib\n"
    "    matplotlib.use('Agg')\n"
    "    matplotlib.rcParams.update({\n"
    "        'font.sans-serif': ['SimHei', 'Microsoft YaHei', 'Noto Sans CJK SC', 'DejaVu Sans'],\n"
    "        'axes.unicode_minus': False,\n"
    "        'figure.dpi': 100,\n"
    "        'savefig.dpi': 150,\n"
    "        'savefig.bbox': 'tight',\n"
    "    })\n"
    "except ImportError:\n"
    "    pass\n"
)


def _seed_header(seed: int) -> str:
    return (
        "import random as _random\n"
        "try:\n"
        "    from numpy import random as _np_random\n"
        f"    _np_random.seed({int(seed)})\n"
        "except ImportError:\n"
        "    pass\n"
        f"_random.seed({int(seed)})\n"
    )


def run_once(script: Path, timeout: int = 60, seed: int | None = 42,
             figures_dir: Path | None = None, keep: bool = False) -> dict:
    """在隔离临时目录执行一个求解脚本，返回结构化结果。

    返回字段：status(ok/run_error/timeout/safety_fail/selftest_fail/empty)、
    stdout、stderr、metrics_json、figures、returncode、issues、tmp_dir。
    """
    raw = script.read_text(encoding="utf-8", errors="replace")
    code = normalize_data_paths(sanitize_code(raw))

    is_safe, safety_error = validate_code(code)
    if not is_safe:
        return {"status": "safety_fail", "stdout": "", "stderr": safety_error,
                "metrics_json": {}, "figures": [], "returncode": -1,
                "issues": [], "tmp_dir": None}

    missing = check_dependencies(code)
    tmp_dir = Path(tempfile.mkdtemp(prefix="modelsmith_exec_"))
    try:
        (tmp_dir / "solution.py").write_text(_HEADER + _seed_header(seed) + code,
                                             encoding="utf-8")
        data_copied = _copy_data_context(script.resolve().parent, tmp_dir)
        (tmp_dir / "metrics.json").unlink(missing_ok=True)

        out_file, err_file = tmp_dir / "_stdout.txt", tmp_dir / "_stderr.txt"
        timed_out = False
        with open(out_file, "wb") as fo, open(err_file, "wb") as fe:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(tmp_dir / "solution.py")],
                stdout=fo, stderr=fe, stdin=subprocess.DEVNULL, cwd=str(tmp_dir),
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                proc.wait()
                timed_out = True
                returncode = proc.returncode if proc.returncode is not None else -9

        stdout = out_file.read_text(encoding="utf-8", errors="replace")
        stderr = err_file.read_text(encoding="utf-8", errors="replace")

        metrics_json: dict = {}
        if (tmp_dir / "metrics.json").exists():
            try:
                parsed = json.loads((tmp_dir / "metrics.json").read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    metrics_json = parsed
            except (json.JSONDecodeError, OSError):
                pass

        figures: list[str] = []
        for ext in (".png", ".jpg", ".jpeg", ".pdf"):
            for p in tmp_dir.glob(f"*{ext}"):
                if figures_dir is not None:
                    figures_dir.mkdir(parents=True, exist_ok=True)
                    dest = figures_dir / p.name
                    shutil.copy2(p, dest)
                    figures.append(str(dest))
                else:
                    figures.append(str(p))

        issues: list[dict] = []
        range_issue = check_expected_ranges(metrics_json)
        if range_issue:
            issues.append(range_issue)
        _mj_numbers = metrics_json.get("numbers")
        merged_numbers = {
            **(_mj_numbers if isinstance(_mj_numbers, dict) else {}),
            **extract_metrics(stdout)["numbers"],
        }
        sanity_issue = check_result_sanity(
            {**metrics_json, "numbers": merged_numbers}, stdout)
        if sanity_issue:
            issues.append(sanity_issue)
        if not has_selftest(raw):
            issues.append({
                "problem": "missing_selftest",
                "detail": "脚本未定义 _selftest() 自检函数（用题目已知条件验证核心计算）",
                "hint": (
                    "增加 _selftest()：用题目给的已知条件算一个可手算验证的值，"
                    "__main__ 第一步运行它，失败打印 SELFTEST FAIL 并 sys.exit(1)——"
                    "这是「跑通但结果错」的第一道防线"
                ),
            })

        stdout_is_error = any(pat in stdout for pat in ERROR_PATTERNS)
        selftest_failed = "SELFTEST FAIL" in stdout
        has_numbers = bool(extract_metrics(stdout)["numbers"])
        empty_success = (
            returncode == 0 and not metrics_json and not figures
            and not has_numbers and not has_real_computation(raw)
        )
        if timed_out:
            status = "timeout"
        elif returncode != 0:
            status = "run_error"
        elif selftest_failed:
            status = "selftest_fail"
        elif stdout_is_error or empty_success:
            status = "empty"
        else:
            status = "ok"

        return {"status": status, "stdout": stdout, "stderr": stderr,
                "metrics_json": metrics_json, "figures": figures,
                "returncode": returncode, "issues": issues,
                "data_copied": data_copied,
                "tmp_dir": str(tmp_dir) if keep else None}
    finally:
        if not keep:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _selftest() -> int:
    """内置自检：安全扫描/区间校验/零键/指标提取各一道题。"""
    checks: list[tuple[str, bool]] = []

    ok, err = validate_code("import os\nos.system('rm -rf /')\n")
    checks.append(("安全扫描拦截 os.system", not ok and "危险" in err))
    ok, _ = validate_code("import numpy as np\nx = np.linspace(0, 1, 10)\nprint(x.mean())\n")
    checks.append(("安全扫描放行正常代码", ok))
    ok, _ = validate_code("y = eval('1+1')\n")
    checks.append(("安全扫描拦截 eval", not ok))

    issue = check_expected_ranges({"速度": 25.0,
                                   "_expected_ranges": {"速度": [0, 10]}})
    checks.append(("区间校验检出超范围", issue is not None))
    checks.append(("范围内不误报",
                   check_expected_ranges({"速度": 5.0,
                                          "_expected_ranges": {"速度": [0, 10]}}) is None))

    issue = check_result_sanity(
        {"numbers": {"遮蔽时长": [0.0]}}, "")
    checks.append(("强零键检出全零", issue is not None and issue["problem"] == "zero_value"))
    checks.append(("弱键为零但有非零弱键不告警",
                   check_result_sanity({"numbers": {"距离": [0.0], "收益": [5.0]}}, "") is None))

    m = extract_metrics("最优值: 42.5\n误差 = 3.2\n")
    checks.append(("指标提取数值对", m["numbers"].get("最优值") == [42.5]))

    checks.append(("has_selftest 检出",
                   has_selftest("def _selftest():\n    return True\n") is True))
    checks.append(("has_selftest 缺失",
                   has_selftest("def main():\n    pass\n") is False))

    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        print(f"  {'✓' if passed else '✗ FAIL'} {name}")
    if failed:
        print(f"自检失败 {len(failed)} 项")
        return 1
    print(f"自检通过（{len(checks)} 项）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", nargs="?", help="求解脚本路径")
    ap.add_argument("--algo", default=None, help="算法名（用于动态超时/随机类提示）")
    ap.add_argument("--timeout", type=int, default=None, help="超时秒数（默认按算法解析，兜底 60）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    ap.add_argument("--figures-dir", default="figures", help="图片持久化目录（默认 ./figures）")
    ap.add_argument("--keep", action="store_true", help="保留沙箱临时目录（调试用）")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.script:
        ap.print_help()
        return 2

    script = Path(args.script)
    if not script.exists():
        print(f"脚本不存在: {script}")
        return 2
    timeout = resolve_timeout(args.algo, args.timeout)
    stochastic = bool(args.algo) and any(k in args.algo for k in STOCHASTIC_ALGOS)

    result = run_once(script, timeout=timeout, seed=args.seed,
                      figures_dir=Path(args.figures_dir), keep=args.keep)

    print("=" * 60)
    print(f"沙箱执行: {script.name}  (timeout={timeout}s, seed={args.seed})")
    print(f"状态: {result['status']}  returncode={result['returncode']}")
    if result["stderr"].strip():
        print(f"stderr 尾部: {result['stderr'][-500:]}")
    if result["issues"]:
        for iss in result["issues"]:
            print(f"[警告] {iss['problem']}: {iss['detail']}")
            print(f"  修复方向: {iss['hint']}")
    if result["status"] == "ok" and stochastic:
        print("[提示] 随机类算法：单次运行不可作数，必须再跑 "
              f"python scripts/solve/run_stability.py {script.name} --algo {args.algo}")
    if result["figures"]:
        print(f"图片: {len(result['figures'])} 张 → {args.figures_dir}/")
    clean_metrics = {k: v for k, v in result["metrics_json"].items()
                     if not (isinstance(k, str) and k.startswith("_"))}
    print("-" * 60)
    print(json.dumps({
        "status": result["status"],
        "returncode": result["returncode"],
        "metrics_json": clean_metrics,
        "issues": result["issues"],
        "figures": result["figures"],
        "stochastic_algo": stochastic,
        "seed": args.seed,
    }, ensure_ascii=False, indent=2))
    passed = result["status"] == "ok" and not result["issues"]
    return 0 if passed else (2 if result["status"] == "safety_fail" else 1)


if __name__ == "__main__":
    sys.exit(main())
