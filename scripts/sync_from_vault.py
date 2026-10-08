#!/usr/bin/env python3
"""从 quant-wiki vault 的 `wiki/` 同步一份「可公开」的内容到 Quartz 的 `content/`。

设计原则
--------
- **只读 vault**：本脚本绝不写入 vault，只读 `wiki/` 并写 `content/`。
- **fail closed**：解析不出 vault 直接报错，不猜路径。
- **白名单**：frontmatter 只保留 `frontmatterKeep` 里的键，其余（含内部 id、
  `stage`、`locator` 等）一律剥除，避免本机路径与内部状态外泄。
- **不公开的源**：`[[source-docs/X|Y]]` 这类指向非公开目录的 wikilink，
  降级成纯文本 `Y`，否则会满站死链，且等于变相公开研报目录。

用法
----
    python scripts/sync_from_vault.py                # 用 publish.config.json
    python scripts/sync_from_vault.py --vault D:\\MyVault
    python scripts/sync_from_vault.py --dry-run      # 只报告，不落盘
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from collections import defaultdict

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PRODUCT_ROOT / "publish.config.json"

# 疑似本机绝对路径（脱敏自检用）。
# 只认 Windows 盘符路径，并要求路径段内不出现空格、括号、花括号、反斜杠。
# 这样 LaTeX 的 \\[4pt、\\{...\\}、\\alpha 都不会被误判成本机路径。
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'|)\]{}\[\]\\]+")

# [[target|alias]] 或 [[target]]
WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|([^\]]*?))?\]\]")

FRONTMATTER_DELIM = "---"

# Quartz 支持的 callout 类型（见 quartz/styles/callouts.scss）
CALLOUT_KINDS = {
    "note", "abstract", "info", "todo", "tip", "success",
    "question", "warning", "failure", "danger", "bug", "example", "quote",
}


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass
class DemoNotice:
    """演示版本提示的配置。"""
    enabled: bool = False
    scope: str = "all"          # "all" = 每页都加；"index" = 只在首页加
    kind: str = "info"          # callout 类型
    sentinel: str = "DEMO-NOTICE"  # 幂等标记（写入注释里）
    page_title: str = ""
    page_lines: list[str] = field(default_factory=list)
    index_title: str = ""
    index_lines: list[str] = field(default_factory=list)


@dataclass
class PublishConfig:
    vault: Path
    content_dir: Path
    exclude_dirs: list[str]
    exclude_files: list[str]
    frontmatter_keep: list[str]
    redact_prefixes: list[str]
    curated: list[str]
    regenerate_index: bool = True
    notice: DemoNotice = field(default_factory=DemoNotice)
    static_files: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False
    # 运行期统计
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    redacted_links: int = 0
    notices_added: int = 0
    statics_written: list[str] = field(default_factory=list)
    index_regenerated: bool = False
    suspect_paths: list[tuple[str, str]] = field(default_factory=list)


def load_config(argv: list[str] | None = None) -> PublishConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", help="vault 根目录（含 .quant-wiki.json）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="发布配置文件")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写 content/")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_file():
        raise ConfigError(f"找不到发布配置：{config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    # 优先级：显式 --vault > QUANT_WIKI_VAULT > 配置文件
    vault_raw = args.vault or os.environ.get("QUANT_WIKI_VAULT") or raw.get("vault")
    if not vault_raw:
        raise ConfigError(
            "未解析出 vault 路径。请传 --vault，或设 QUANT_WIKI_VAULT，"
            f"或在 {config_path} 里填 vault 字段。"
        )
    vault = Path(vault_raw).expanduser().resolve()

    marker = vault / ".quant-wiki.json"
    if not marker.is_file():
        raise ConfigError(f"{vault} 下没有 .quant-wiki.json，不是一个 quant-wiki vault。")

    content_dir = PRODUCT_ROOT / raw.get("contentDir", "content")

    # 演示提示配置
    n = raw.get("demoNotice") or {}
    notice = DemoNotice(
        enabled=bool(n.get("enabled", False)),
        scope=str(n.get("scope", "all")),
        kind=str(n.get("kind", "info")),
        sentinel=str(n.get("sentinel", "DEMO-NOTICE")),
        page_title=str(n.get("pageTitle", "")),
        page_lines=[str(x) for x in (n.get("pageLines") or [])],
        index_title=str(n.get("indexTitle", "")),
        index_lines=[str(x) for x in (n.get("indexLines") or [])],
    )
    if notice.enabled and notice.scope not in ("all", "index"):
        raise ConfigError(f"demoNotice.scope 只能是 'all' 或 'index'，收到 {notice.scope!r}")
    if notice.enabled and notice.kind not in CALLOUT_KINDS:
        raise ConfigError(
            f"demoNotice.kind={notice.kind!r} 不是受支持的 callout 类型；"
            f"可用：{', '.join(sorted(CALLOUT_KINDS))}"
        )

    # 随内容一起产出的静态文件（robots.txt / _headers 等）。
    # ⚠️ 必须由本脚本生成：main() 会先 rmtree(content_dir)，
    # 手工放进去的静态文件每次同步都会被抹掉（曾经因此丢掉整个 noindex 保护）。
    # 以 `$` 开头的键是注释，跳过。
    static_files = {
        str(k): str(v)
        for k, v in (raw.get("staticFiles") or {}).items()
        if not str(k).startswith("$")
    }

    curated = [str(x) for x in (raw.get("curated") or []) if str(x).strip()]

    return PublishConfig(
        vault=vault,
        content_dir=content_dir,
        exclude_dirs=list(raw.get("excludeDirs", [])),
        exclude_files=list(raw.get("excludeFiles", [])),
        frontmatter_keep=list(raw.get("frontmatterKeep", ["title"])),
        redact_prefixes=list(raw.get("redactWikiLinkPrefixes", [])),
        curated=curated,
        regenerate_index=bool(raw.get("regenerateIndex", True)),
        notice=notice,
        static_files=static_files,
        dry_run=args.dry_run,
    )


def strip_frontmatter(text: str, keep: list[str]) -> str:
    """按白名单重写 frontmatter，其余键（含其缩进块）整段剥除。

    刻意用手写逐行处理而非 yaml round-trip：避免重排中文、改写列表风格，
    也避免把不支持的类型静默转成字符串。
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end = i
            break
    if end is None:
        return text

    body, tail = lines[1:end], lines[end + 1 :]

    kept: list[str] = []
    skipping = False
    for line in body:
        stripped = line.strip()
        if not stripped:
            skipping = False
            continue
        # 顶层键：非空白起始且形如 key:
        is_toplevel = not line[:1].isspace() and ":" in line
        if is_toplevel:
            key = line.split(":", 1)[0].strip()
            skipping = key not in keep
            if not skipping:
                kept.append(line)
        elif not skipping:
            # 被保留键的缩进块（如 tags 的 `  - xxx`）
            kept.append(line)

    # 重新拼装：只保留开头的分隔线，丢掉原始整段，换成白名单后的键。
    # ⚠️ 这里若写成 head（= lines[:end+1]，含原 frontmatter 全文）会把
    # 原始键与白名单行**一起**留在文中，白名单行还会被渲染成正文可见文字。
    out = lines[:1] + kept + lines[end : end + 1] + tail
    return "".join(out)


def norm_link_target(raw: str) -> str:
    """规范化 wikilink 目标。

    ⚠️ Markdown 表格里 `[[目标|别名]]` 必须写成 `[[目标\\|别名]]`，
    正则会把转义产生的尾部反斜杠吃进目标里，必须去掉，
    否则这些链接会被误判为「无法解析」（实测约 310 条）。
    """
    return raw.split("#", 1)[0].strip().rstrip("\\").strip()


class LinkIndex:
    """vault 内的页面索引，用于判断某个 wikilink 指向的页是否被收录。

    需要按「全路径」与「文件名」两种方式解析：Quartz 的链接解析用的是
    shortest 策略，`[[凸显权重]]` 也能命中 `patterns/fields/凸显权重`。
    """

    def __init__(self, wiki_root: Path, exclude_dirs: set[str], exclude_files: set[str]) -> None:
        self.keys: set[str] = set()
        self.by_base: dict[str, list[str]] = defaultdict(list)
        for p in sorted(wiki_root.rglob("*.md")):
            rel = p.relative_to(wiki_root).as_posix()
            if set(rel.split("/")[:-1]) & exclude_dirs:
                continue
            if p.name in exclude_files or p.name.startswith("."):
                continue
            key = rel[:-3]
            self.keys.add(key)
            self.by_base[key.rsplit("/", 1)[-1]].append(key)

    def resolve(self, target: str) -> str | None:
        """把链接目标解析成页面 key；解析不出来返回 None。"""
        t = norm_link_target(target)
        if not t:
            return None
        if t in self.keys:
            return t
        cands = self.by_base.get(t.rsplit("/", 1)[-1], [])
        return cands[0] if len(cands) == 1 else None


def redact_wikilinks(
    text: str,
    prefixes: list[str],
    keep_keys: set[str] | None = None,
    index: "LinkIndex | None" = None,
) -> tuple[str, int]:
    """把「不该公开」的 wikilink 降级为纯文本，返回 (新文本, 降级次数)。

    两种降级条件（命中任一即降级）：
    1. 目标在 `prefixes` 指定的目录下（source-docs / meta 等）
    2. 传入了 keep_keys（抽稀模式）且目标解析到的页**不在**收录集合内

    不处理会导致页面上出现大量半透明的 broken 链接（Quartz 对
    解析不到的内部链接会加 `broken` 类，视觉上是「坏掉的链接」）。
    """
    if not prefixes and keep_keys is None:
        return text, 0
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        raw = match.group(1)
        alias = match.group(2)
        head = raw.split("/", 1)[0].split("#", 1)[0].strip()

        drop = head in prefixes
        if not drop and keep_keys is not None and index is not None:
            resolved = index.resolve(raw)
            # 解析不到的一般是笔误或内部锚点，保持原样交给 Quartz
            if resolved is not None and resolved not in keep_keys:
                drop = True

        if not drop:
            return match.group(0)

        count += 1
        if alias is not None:
            return alias.strip()
        # 无别名时用末段（去掉锚点与转义反斜杠）
        return norm_link_target(raw).rsplit("/", 1)[-1].strip()

    return WIKILINK_RE.sub(repl, text), count


def scan_absolute_paths(rel: str, text: str) -> list[tuple[str, str]]:
    """自检：正文里若仍残留本机绝对路径，回报（脱敏兜底网）。"""
    return [(rel, m.group(0)) for m in ABS_PATH_RE.finditer(text)]


def build_callout(kind: str, title: str, lines: list[str], sentinel: str = "") -> str:
    """生成 Quartz/Obsidian callout 文本块。

    HTML 注释里带 sentinel，便于幂等判断与事后检索。
    """
    out = []
    if sentinel:
        out.append(f"<!-- {sentinel} -->")
    head = f"> [!{kind}]" + (f" {title}" if title else "")
    out.append(head)
    for ln in lines:
        out.append(f"> {ln}" if ln.strip() else ">")
    return "\n".join(out) + "\n\n"


def insert_callout(text: str, block: str, sentinel: str = "") -> str:
    """把提示块插到 frontmatter 之后（没有 frontmatter 就放最前）。

    幂等：已含 sentinel 则不重复插入。
    """
    if sentinel and sentinel in text:
        return text

    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == FRONTMATTER_DELIM:
        for i in range(1, len(lines)):
            if lines[i].strip() == FRONTMATTER_DELIM:
                head = "".join(lines[: i + 1])
                rest = "".join(lines[i + 1 :]).lstrip("\n")
                return f"{head}\n{block}{rest}"
    return block + text


def notice_for(rel: Path, notice: DemoNotice) -> str | None:
    """按配置决定这一页要不要加提示、加哪一版。"""
    if not notice.enabled:
        return None

    is_index = rel.as_posix() == "index.md"

    # 首页且配了详版 → 用详版
    if is_index and notice.index_lines:
        return build_callout(notice.kind, notice.index_title, notice.index_lines, notice.sentinel)

    # 其余页面：仅当 scope=all 时加简版
    if notice.scope == "all" and notice.page_lines:
        return build_callout(notice.kind, notice.page_title, notice.page_lines, notice.sentinel)

    return None


# 各层在首页里的展示名与排序权重
LAYER_META: list[tuple[str, str]] = [
    ("alpha", "收益来源（Alpha）"),
    ("concepts", "理论概念（Concepts）"),
    ("data-semantics/量价", "数据语义 · 量价"),
    ("data-semantics/基本面", "数据语义 · 基本面"),
    ("data-semantics", "数据语义 · 其他"),
    ("paradigms", "构造范式（Paradigms）"),
    ("patterns/fields", "可复用构件 · 字段（Fields）"),
    ("patterns/ops", "可复用构件 · 算子（Ops）"),
    ("patterns/agg", "可复用构件 · 聚合（Agg）"),
    ("patterns/mask", "可复用构件 · 掩码（Mask）"),
    ("patterns", "可复用构件 · 其他"),
]


def layer_of(key: str) -> str:
    """把页面 key 归到一个展示层。"""
    p = key.split("/")
    if p[0] == "patterns" and len(p) >= 2:
        return "patterns/ops" if p[1] == "ops" else f"patterns/{p[1]}"
    if p[0] == "data-semantics":
        return f"data-semantics/{p[1]}" if len(p) >= 2 else "data-semantics"
    return p[0]


def generate_index(keep_keys: set[str], total_keys: int, vault_rel: str = "wiki") -> str:
    """为抽稀后的子集生成首页 index.md。

    原 index 链向全部页面，抽稀后自身就会制造大量断链，因此必须重写。
    按层分组并标注收录规模，让它是「刻意设计的入口」而非残缺目录。
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for k in keep_keys:
        if k == "index":
            continue
        buckets[layer_of(k)].append(k)

    lines: list[str] = []
    lines.append("---")
    lines.append("title: Wiki Index")
    lines.append("type: meta")
    lines.append("tags:")
    lines.append("  - meta")
    lines.append("  - index")
    lines.append("---")
    lines.append("")
    lines.append("# 量化因子知识库")
    lines.append("")
    lines.append(
        f"> 本演示版收录 **{len(keep_keys)} 页**（原始规模 {total_keys} 页），"
        f"按知识层级组织。"
    )
    lines.append("")
    lines.append(
        "知识库按「理论概念 → 收益来源 → 构造范式 → 可复用构件」四层实体建模，"
        "并以数据语义作为底层参照；页面之间用双向链接构成因子知识图谱。"
    )
    lines.append("")

    shown: set[str] = set()
    for layer, title in LAYER_META:
        ks = buckets.get(layer)
        if not ks:
            continue
        shown.add(layer)
        ks = sorted(ks)
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"共 {len(ks)} 页")
        lines.append("")
        for k in ks:
            label = k.rsplit("/", 1)[-1]
            lines.append(f"- [[{k}|{label}]]")
        lines.append("")

    # 兜底：没被 LAYER_META 覆盖的层
    rest = {k: v for k, v in buckets.items() if k not in shown and v}
    if rest:
        lines.append("## 其他")
        lines.append("")
        for layer in sorted(rest):
            for k in sorted(rest[layer]):
                lines.append(f"- [[{k}|{k.rsplit('/', 1)[-1]}]]")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> [!info] 关于本演示版\n"
        "> 以上为**部分页面**。完整知识库还包含研报源文归档、"
        "内部元数据与冲突记录，以及更多构造范式与可复用构件，未在此公开。"
    )
    return "\n".join(lines) + "\n"


def selected(rel: Path, cfg: PublishConfig) -> bool:
    parts = set(rel.parts[:-1])
    if parts & set(cfg.exclude_dirs):
        return False
    if rel.name in cfg.exclude_files:
        return False
    if rel.name.startswith("."):
        return False
    if cfg.curated:
        return rel.as_posix() in cfg.curated
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_config(argv)
    except ConfigError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 2

    wiki = cfg.vault / "wiki"
    if not wiki.is_dir():
        print(f"[fail] {wiki} 不存在。", file=sys.stderr)
        return 2

    if not cfg.dry_run:
        if cfg.content_dir.exists():
            shutil.rmtree(cfg.content_dir)
        cfg.content_dir.mkdir(parents=True, exist_ok=True)

    # 抽稀模式：把 curated 清单转成 key 集合（配置里带 .md，key 不带）
    keep_keys: set[str] | None = None
    if cfg.curated:
        keep_keys = {p[:-3] if p.endswith(".md") else p for p in cfg.curated}

    # 全量页面索引，用于判断链接指向的页是否被收录
    index = LinkIndex(wiki, set(cfg.exclude_dirs), set(cfg.exclude_files))

    if keep_keys is not None:
        unknown = sorted(k for k in keep_keys if k not in index.keys)
        if unknown:
            print(f"[fail] curated 里有 {len(unknown)} 条在 vault 中找不到：", file=sys.stderr)
            for k in unknown[:10]:
                print(f"  - {k}", file=sys.stderr)
            return 2

    for src in sorted(wiki.rglob("*.md")):
        rel = src.relative_to(wiki)

        # 抽稀模式下 index 由脚本重新生成，跳过 vault 里的原文件
        if keep_keys is not None and cfg.regenerate_index and rel.as_posix() == "index.md":
            cfg.skipped.append(rel.as_posix())
            continue

        if not selected(rel, cfg):
            cfg.skipped.append(rel.as_posix())
            continue

        text = src.read_text(encoding="utf-8")
        text = strip_frontmatter(text, cfg.frontmatter_keep)
        text, n = redact_wikilinks(text, cfg.redact_prefixes, keep_keys, index)
        cfg.redacted_links += n

        block = notice_for(rel, cfg.notice)
        if block:
            text = insert_callout(text, block, cfg.notice.sentinel)
            cfg.notices_added += 1

        cfg.suspect_paths.extend(scan_absolute_paths(rel.as_posix(), text))

        if not cfg.dry_run:
            dst = cfg.content_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        cfg.written.append(rel.as_posix())

    # 抽稀模式下重生首页
    if keep_keys is not None and cfg.regenerate_index:
        body = generate_index(keep_keys, len(index.keys))
        if cfg.notice.enabled and cfg.notice.index_lines:
            body = insert_callout(body, build_callout(
                cfg.notice.kind, cfg.notice.index_title,
                cfg.notice.index_lines, cfg.notice.sentinel), cfg.notice.sentinel)
        if not cfg.dry_run:
            (cfg.content_dir / "index.md").write_text(body, encoding="utf-8")
        cfg.written.append("index.md")
        cfg.index_regenerated = True

    # 静态文件（robots.txt / _headers 等）——必须在此重写，
    # 否则会随上面那次 rmtree 一起消失。
    for name, body in cfg.static_files.items():
        cfg.statics_written.append(name)
        if not cfg.dry_run:
            dst = cfg.content_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(body, encoding="utf-8")

    verb = "将写入" if cfg.dry_run else "已写入"
    print(f"vault      : {cfg.vault}")
    print(f"content    : {cfg.content_dir}")
    if keep_keys is not None:
        print(f"抽稀模式   : curated 收录 {len(keep_keys)} 页"
              f"（vault 共 {len(index.keys)} 页）")
    print(f"{verb}    : {len(cfg.written)} 页")
    print(f"跳过       : {len(cfg.skipped)} 页")
    print(f"wikilink 降级: {cfg.redacted_links} 处")
    print(f"首页重生   : {'是' if cfg.index_regenerated else '否'}")
    if cfg.notice.enabled:
        print(f"演示提示   : 已加 {cfg.notices_added} 页"
              f"（scope={cfg.notice.scope}, kind={cfg.notice.kind}）")
    else:
        print("演示提示   : 未启用")
    if cfg.static_files:
        print(f"静态文件   : {', '.join(cfg.statics_written)}")
    else:
        print("静态文件   : 无")
    if cfg.suspect_paths:
        print(f"\n[warn] 正文仍含疑似本机绝对路径 {len(cfg.suspect_paths)} 处，请检查：")
        for rel, hit in cfg.suspect_paths[:20]:
            print(f"  - {rel}: {hit}")
    else:
        print("绝对路径自检: 通过（未发现残留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
