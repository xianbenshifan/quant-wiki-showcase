---
title: A股市值
created: 2026-09-16
updated: 2026-09-16
tags:
  - field
aliases: [a_share_market_val, a-share market value]
---

<!-- DEMO-NOTICE -->
> [!info] 演示版本
> 本站为**项目演示版本**，仅收录知识库的**部分页面**，部分内容可能不完整。

## 金融解释

A 股口径的市值。本项由财务报表科目加工而来，数据源 `rqf_der_*`（衍生表结构性科目）直接提供该列，加工公式给出其标准金融定义，便于复现时核对口径。

## 加工公式

$\text{A股市值} = \text{A股收盘价} \times \text{A股股本}$
