#!/usr/bin/env python3
"""查询 Cloudflare Web Analytics（RUM）的访问明细。

面板只给汇总，本脚本能列出**逐条**路径 / 时间 / 国家 / 浏览器，
用来回答「这个站点什么时候被访问过、看了哪些页」。

数据来源：Cloudflare GraphQL Analytics API。
凭证默认取自 wrangler 的 OAuth 配置（无需额外配置）。

⚠️ 两个实测得到的坑，脚本已处理：

1. **数据不是实时的**（实测入库延迟约 4–5 分钟）。刚访问完立刻查会是空的，
   不要据此判断「没统计到」。
2. **beacon 里的 token 与 RUM 的 siteTag 不是同一个值**，同一站点可能出现
   多个 siteTag。因此本脚本默认**不按 siteTag 过滤**，而是列出所有有数据的
   siteTag 并聚合展示；想只看某一个再用 `--site-tag` 指定。

用法
----
    python query_rum.py                      # 近 7 天明细
    python query_rum.py --days 1             # 只看今天
    python query_rum.py --by-path            # 按路径聚合
    python query_rum.py --site-tag <tag>     # 只看某个 siteTag
    python query_rum.py --json               # 输出 JSON

配置解析顺序（高 → 低）：命令行参数 → 环境变量 → 配置文件 → 报错。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PRODUCT_ROOT / "rum.config.json"

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

# wrangler 的 OAuth 凭证（登录 wrangler 后自动生成）
WRANGLER_CONFIGS = [
    Path(os.path.expandvars(r"%APPDATA%\xdg.config\.wrangler\config\default.toml")),
    Path.home() / ".config" / ".wrangler" / "config" / "default.toml",
    Path.home() / "Library" / "Preferences" / ".wrangler" / "config" / "default.toml",
]

QUERY = """
query($acct: String!, $since: Date!, $until: Date!) {
  viewer {
    accounts(filter: {accountTag: $acct}) {
      rumPageloadEventsAdaptiveGroups(
        limit: 1000
        filter: {date_geq: $since, date_leq: $until}
        orderBy: [date_DESC]
      ) {
        count
        dimensions { date siteTag requestPath countryName userAgentBrowser }
      }
    }
  }
}
"""


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_oauth_token(auto_refresh: bool = True) -> str:
    """取 Cloudflare API 凭证：环境变量优先，其次 wrangler 的 OAuth 配置。

    wrangler 的 OAuth 凭证**有有效期**（约 1 小时）。过期后 API 会返回 401。
    这里会检测过期并在需要时自动跑一次 `wrangler whoami` 触发刷新，
    避免「昨天还能用、今天突然 401」这种莫名其妙的失败。
    """
    env = os.environ.get("CLOUDFLARE_API_TOKEN")
    if env:
        return env

    try:
        import tomllib
    except ImportError:
        raise ConfigError("Python 需要 3.11+ 才能读取 wrangler 配置；"
                          "或改用 CLOUDFLARE_API_TOKEN 环境变量。")

    def read() -> tuple[str | None, str | None]:
        for p in WRANGLER_CONFIGS:
            if not p.is_file():
                continue
            cfg = tomllib.loads(p.read_text(encoding="utf-8"))
            tok = cfg.get("oauth_token") or cfg.get("api_token")
            if tok:
                return tok, cfg.get("expiration_time")
        return None, None

    tok, exp = read()
    if tok is None:
        raise ConfigError(
            "找不到 Cloudflare 凭证。请先 `npx wrangler login`，"
            "或设置环境变量 CLOUDFLARE_API_TOKEN。"
        )

    expired = False
    if exp:
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            expired = t <= datetime.now(timezone.utc)
        except Exception:
            expired = False

    if expired and auto_refresh:
        print("[info] wrangler 凭证已过期，正在刷新（npx wrangler whoami）…",
              file=sys.stderr)
        try:
            import subprocess
            subprocess.run(["npx", "--yes", "wrangler@latest", "whoami"],
                           capture_output=True, timeout=180, shell=(os.name == "nt"))
        except Exception as exc:
            print(f"[warn] 自动刷新失败：{exc}", file=sys.stderr)
        tok, exp = read()
        if exp:
            try:
                from datetime import datetime, timezone
                t = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if t <= datetime.now(timezone.utc):
                    print(f"[warn] 刷新后凭证仍然过期（{exp}）。"
                          f"请手动运行 `npx wrangler login`。", file=sys.stderr)
            except Exception:
                pass

    return tok or ""


def load_account(argv_account: str | None) -> tuple[str, str | None]:
    """解析 accountTag 与可选 siteTag。返回 (account, site_tag)。"""
    site_env = os.environ.get("QUANT_WIKI_RUM_SITE_TAG")
    account = argv_account or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    site = site_env
    cfg = {}
    if DEFAULT_CONFIG.is_file():
        try:
            cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{DEFAULT_CONFIG} 不是合法 JSON：{e}")
    account = account or cfg.get("accountTag")
    site = site or cfg.get("siteTag")
    if not account:
        raise ConfigError(
            "未解析出 accountTag。请传 --account，或设 CLOUDFLARE_ACCOUNT_ID，"
            f"或在 {DEFAULT_CONFIG} 里写 accountTag。"
        )
    return account, site


def gql(token: str, account: str, since: date, until: date) -> list[dict]:
    payload = json.dumps({
        "query": QUERY,
        "variables": {"acct": account, "since": since.isoformat(),
                      "until": until.isoformat()},
    }).encode()
    req = urllib.request.Request(
        GRAPHQL_URL, data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ConfigError(
            f"Cloudflare API 返回 HTTP {e.code}："
            f"{e.read().decode('utf-8', 'replace')[:300]}"
        ) from e
    if resp.get("errors"):
        raise ConfigError("GraphQL 报错：" + json.dumps(resp["errors"], ensure_ascii=False)[:400])
    accts = ((resp.get("data") or {}).get("viewer", {}) or {}).get("accounts") or []
    if not accts:
        return []
    return accts[0].get("rumPageloadEventsAdaptiveGroups") or []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="查询近几天（默认 7）")
    ap.add_argument("--account", help="Cloudflare accountTag")
    ap.add_argument("--site-tag", dest="site_tag", help="只统计某个 siteTag")
    ap.add_argument("--by-path", action="store_true", help="按路径聚合")
    ap.add_argument("--by-day", action="store_true", help="按日期聚合")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    try:
        token = load_oauth_token()
        account, cfg_site = load_account(args.account)
    except ConfigError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 2

    site = args.site_tag or cfg_site
    until = date.today()
    since = until - timedelta(days=max(args.days - 1, 0))

    try:
        rows = gql(token, account, since, until)
    except ConfigError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1

    # 按 siteTag 过滤（可选）
    if site:
        rows = [r for r in rows if (r.get("dimensions") or {}).get("siteTag") == site]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("=" * 82)
    print(f"Web Analytics 访问记录   {since} ~ {until}   账号 {account[:8]}…")
    if site:
        print(f"（已按 siteTag 过滤：{site}）")
    print("=" * 82)

    if not rows:
        print("\n  无记录。")
        print("  提示：入库有约 4–5 分钟延迟，刚访问完请稍等再查；")
        print("        若始终为空，检查账号下是否有该站点的 Web Analytics 条目。")
        return 0

    total = sum(r.get("count", 0) for r in rows)

    # siteTag 分布（常出现多个，值得提示）
    tags: dict[str, int] = defaultdict(int)
    for r in rows:
        tags[(r.get("dimensions") or {}).get("siteTag") or "(无)"] += r.get("count", 0)
    if len(tags) > 1:
        print("\n  发现多个 siteTag（同一站点可能出现多个，注意别只看其中一个）：")
        for t, n in sorted(tags.items(), key=lambda x: -x[1]):
            print(f"    {t}   {n} 次")

    print(f"\n  总页面浏览：{total} 次，共 {len(rows)} 条聚合记录\n")

    if args.by_path:
        agg: dict[str, int] = defaultdict(int)
        for r in rows:
            agg[(r.get("dimensions") or {}).get("requestPath") or "(未知)"] += r.get("count", 0)
        print(f"  {'次数':>6}  路径")
        for p, n in sorted(agg.items(), key=lambda x: -x[1]):
            print(f"  {n:>6}  {p[:70]}")
    elif args.by_day:
        agg = defaultdict(int)
        for r in rows:
            agg[(r.get("dimensions") or {}).get("date") or "(未知)"] += r.get("count", 0)
        print(f"  {'次数':>6}  日期")
        for d, n in sorted(agg.items(), reverse=True):
            print(f"  {n:>6}  {d}")
    else:
        print(f"  {'日期':<12}{'次数':>5}  {'国家':<8}{'浏览器':<10}路径")
        for r in sorted(rows, key=lambda x: ((x.get("dimensions") or {}).get("date") or ""),
                        reverse=True):
            d = r.get("dimensions") or {}
            print(f"  {str(d.get('date')):<12}{r.get('count', 0):>5}  "
                  f"{str(d.get('countryName') or '?')[:7]:<8}"
                  f"{str(d.get('userAgentBrowser') or '?')[:9]:<10}"
                  f"{str(d.get('requestPath'))[:44]}")

    print()
    print("  提示：404 页面不注入统计脚本，因此不存在的路径不会被计入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
