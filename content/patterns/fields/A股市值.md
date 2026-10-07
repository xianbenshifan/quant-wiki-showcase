---
title: A股市值
pattern_id: A股市值
type: field
status: draft
created: 2026-09-16
updated: 2026-09-16
tags:
  - field
factor_domain: [fundamental]
aliases: [a_share_market_val, a-share market value]
finance_explanation: A 股口径的市值
derived_from: ["股东与股本", "价格"]
mutable_axes: 计算口径（数据源直接提供 / 自行加工）；对齐时点（报告期 / 公告日）
stage: draft
---
title: A股市值
created: 2026-09-16
updated: 2026-09-16
tags:
  - field
aliases: [a_share_market_val, a-share market value]

## 金融解释

A 股口径的市值。本项由财务报表科目加工而来，数据源 `rqf_der_*`（衍生表结构性科目）直接提供该列，加工公式给出其标准金融定义，便于复现时核对口径。

## 加工公式

$\text{A股市值} = \text{A股收盘价} \times \text{A股股本}$
