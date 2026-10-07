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

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PRODUCT_ROOT / "publish.config.json"

# 疑似本机绝对路径（脱敏自检用）。
# 只认 Windows 盘符路径，并要求路径段内不出现空格、括号、花括号、反斜杠。
# 这样 LaTeX 的 \\[4pt、\\{...\\}、\\alpha 都不会被误判成本机路径。
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'|)\]{}\[\]\\]+")

# [[target|alias]] 或 [[target]]
WIKILINK_RE = re.compile(r"\[\[([^\]\|]+?)(?:\|([^\]]*?))?\]\]")

FRONTMATTER_DELIM = "---"


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


@dataclass
class PublishConfig:
    vault: Path
    content_dir: Path
    exclude_dirs: list[str]
    exclude_files: list[str]
    frontmatter_keep: list[str]
    redact_prefixes: list[str]
    curated: list[str]
    dry_run: bool = False
    # 运行期统计
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    redacted_links: int = 0
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

    return PublishConfig(
        vault=vault,
        content_dir=content_dir,
        exclude_dirs=list(raw.get("excludeDirs", [])),
        exclude_files=list(raw.get("excludeFiles", [])),
        frontmatter_keep=list(raw.get("frontmatterKeep", ["title"])),
        redact_prefixes=list(raw.get("redactWikiLinkPrefixes", [])),
        curated=list(raw.get("curated", [])),
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

    head, body, tail = lines[: end + 1], lines[1:end], lines[end + 1 :]

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

    out = head + kept + tail
    return "".join(out)


def redact_wikilinks(text: str, prefixes: list[str]) -> tuple[str, int]:
    """把指向非公开目录的 wikilink 降级为纯文本，返回 (新文本, 降级次数)。"""
    if not prefixes:
        return text, 0
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        target = match.group(1).strip()
        alias = match.group(2)
        head = target.split("/", 1)[0].split("#", 1)[0].strip()
        if head not in prefixes:
            return match.group(0)
        count += 1
        if alias is not None:
            return alias.strip()
        # 无别名时用末段，去掉锚点
        return target.split("#", 1)[0].rsplit("/", 1)[-1].strip()

    return WIKILINK_RE.sub(repl, text), count


def scan_absolute_paths(rel: str, text: str) -> list[tuple[str, str]]:
    """自检：正文里若仍残留本机绝对路径，回报（脱敏兜底网）。"""
    return [(rel, m.group(0)) for m in ABS_PATH_RE.finditer(text)]


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

    for src in sorted(wiki.rglob("*.md")):
        rel = src.relative_to(wiki)
        if not selected(rel, cfg):
            cfg.skipped.append(rel.as_posix())
            continue

        text = src.read_text(encoding="utf-8")
        text = strip_frontmatter(text, cfg.frontmatter_keep)
        text, n = redact_wikilinks(text, cfg.redact_prefixes)
        cfg.redacted_links += n
        cfg.suspect_paths.extend(scan_absolute_paths(rel.as_posix(), text))

        if not cfg.dry_run:
            dst = cfg.content_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        cfg.written.append(rel.as_posix())

    verb = "将写入" if cfg.dry_run else "已写入"
    print(f"vault      : {cfg.vault}")
    print(f"content    : {cfg.content_dir}")
    print(f"{verb}    : {len(cfg.written)} 页")
    print(f"跳过       : {len(cfg.skipped)} 页")
    print(f"wikilink 降级: {cfg.redacted_links} 处")
    if cfg.suspect_paths:
        print(f"\n[warn] 正文仍含疑似本机绝对路径 {len(cfg.suspect_paths)} 处，请检查：")
        for rel, hit in cfg.suspect_paths[:20]:
            print(f"  - {rel}: {hit}")
    else:
        print("绝对路径自检: 通过（未发现残留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
