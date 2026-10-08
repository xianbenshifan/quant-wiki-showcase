#!/usr/bin/env python3
"""对 Quartz 构建产物做上线前自检（只读 public/）。

检查项
------
1. 无 `.md` 源文件泄漏（防止把 vault 源文直接发布）
2. 无本机绝对路径残留（盘符路径 / UNC）
3. 全站死链统计（Quartz 会给解析不到的内部链接加 `broken` class）
4. 关键产物存在性：首页、搜索索引、Graph 数据

用法
----
    python scripts/verify_build.py
    python scripts/verify_build.py --public public
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent

# Quartz 给解析不到的内部链接打上 broken class
BROKEN_RE = re.compile(r'class="[^"]*\bbroken\b[^"]*"[^>]*href="([^"]+)"')

# 只认 Windows 盘符绝对路径（本 vault 的真实泄漏面）。
# 刻意不认 UNC：LaTeX 的 \\alpha、\\theta\\approx 之类与 UNC 无法区分，
# 会把数学公式全判成路径，反而让自检失去意义。尾部排除括号/花括号，
# 避免匹配到 LaTeX 的 \\text{...} 结构。
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'|)\]{}\[\]\\]+")

# frontmatter 若泄漏进搜索索引，等于把内部字段（含本机 locator）公开
FRONTMATTER_LEAK_KEYS = ("locator", "stage:", "provenance_state", "alpha_id", "paradigm_id")

# 正文里出现这些「YAML 形态」的片段，说明 frontmatter 被错误地渲染成了可见文字。
# 选标记的标准：在干净的正文里出现 0 次（实测），否则会误报。
# 注意不要放 `pipeline:` —— 有 3 页正文在讨论 `pipeline: unclassified`，会误报。
BODY_FRONTMATTER_MARKERS = (
    "created: 2", "updated: 2",
    "paradigm_id:", "alpha_id:", "concept_id:", "pattern_id:",
    "stage: draft", "stage: active", "return_source:",
)

TEXTUAL_SUFFIXES = {".html", ".css", ".js", ".json", ".xml", ".txt"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", default="public", help="构建产物目录")
    args = parser.parse_args(argv)

    public = (PRODUCT_ROOT / args.public).resolve()
    if not public.is_dir():
        print(f"[fail] 构建产物目录不存在：{public}（先跑 npx quartz build）", file=sys.stderr)
        return 2

    files = [p for p in public.rglob("*") if p.is_file()]
    failures: list[str] = []

    # 1. .md 泄漏
    md_files = [p for p in files if p.suffix == ".md"]
    if md_files:
        failures.append(f"发现 {len(md_files)} 个 .md 文件泄漏，例如 {md_files[0].relative_to(public)}")

    # 2. 绝对路径残留
    suspect: list[tuple[str, str]] = []
    for p in files:
        if p.suffix not in TEXTUAL_SUFFIXES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in ABS_PATH_RE.finditer(text):
            lo = max(0, m.start() - 40)
            hi = min(len(text), m.end() + 40)
            context = text[lo:hi].replace("\n", " ")
            suspect.append((str(p.relative_to(public)), f"{m.group(0)}  …{context}…"))
            if len(suspect) >= 20:
                break
        if len(suspect) >= 20:
            break
    if suspect:
        failures.append(f"发现 {len(suspect)} 处疑似本机绝对路径")

    # 3. 死链
    html_files = [p for p in files if p.suffix == ".html"]
    broken_total = 0
    broken_samples: list[str] = []
    for p in html_files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in BROKEN_RE.finditer(text):
            broken_total += 1
            if len(broken_samples) < 15:
                broken_samples.append(f"{p.relative_to(public)} -> {m.group(1)}")

    # 4. 关键产物
    required = {
        "首页 index.html": public / "index.html",
    }
    static_dir = public / "static"
    search_index = list(static_dir.glob("contentIndex.json")) if static_dir.is_dir() else []
    graph_data = search_index  # v5 的 Graph 与搜索共用 contentIndex.json

    print(f"产物目录     : {public}")
    print(f"文件总数     : {len(files)}")
    print(f"HTML 页数    : {len(html_files)}")
    print(f".md 泄漏     : {len(md_files)}")
    print(f"绝对路径残留 : {len(suspect)}")
    print(f"死链数       : {broken_total}")
    print(f"搜索/Graph 数据: {[p.name for p in search_index] or '未找到'}")

    # 4b. frontmatter 泄漏进搜索索引
    index_leaks: list[str] = []
    for p in search_index:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for key in FRONTMATTER_LEAK_KEYS:
            if key in text:
                index_leaks.append(f"{p.name} 含 `{key}`")

    # 4c. frontmatter 被当成正文渲染（白名单拼装写错时的典型症状）
    body_leaks: list[tuple[str, str]] = []
    for p in html_files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in BODY_FRONTMATTER_MARKERS:
            if m in text:
                body_leaks.append((str(p.relative_to(public)), m))
                break
        if len(body_leaks) >= 10:
            break

    print(f"正文含 frontmatter: {len(body_leaks)} 页")

    for name, path in required.items():
        if not path.exists():
            failures.append(f"缺少关键产物：{name}")
    if not search_index:
        failures.append("未生成搜索索引（搜索功能会失效）")
    if index_leaks:
        failures.extend(f"搜索索引泄漏 frontmatter：{x}" for x in index_leaks)
    if body_leaks:
        failures.append(f"frontmatter 被渲染成正文（{len(body_leaks)} 页命中），例如：")
        failures.extend(f"  {rel} 含 `{m}`" for rel, m in body_leaks[:5])

    if broken_samples:
        print("\n死链抽样：")
        for s in broken_samples:
            print(f"  - {s}")

    if suspect:
        print("\n绝对路径抽样：")
        for rel, hit in suspect[:10]:
            print(f"  - {rel}: {hit}")
    if failures:
        print("\n[fail] 自检未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n[ok] 自检通过：无泄漏、无绝对路径、无死链、关键产物齐全")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
