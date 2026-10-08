# quant-wiki-showcase

把 quant-wiki vault 里的知识库发布成一个**只读的、Obsidian 风格的静态站**，用于简历项目展示。

基于 [Quartz v5](https://quartz.jzhao.xyz/)（MIT）。本仓库只负责「把 vault 里**该公开的**内容抽出来、脱敏、构建成静态产物」，**不是** vault 本身，也**不提供写入能力**。

## 安全边界（先读这段）

- **只读 vault**：`scripts/sync_from_vault.py` 只读 `wiki/`，写的是本仓库的 `content/`，绝不碰 vault。
- **白名单发布**：frontmatter 只保留 `publish.config.json` 里 `frontmatterKeep` 声明的字段，其余（`locator` 本机绝对路径、内部 id、`stage` 等）整段剥除。
- **非公开目录降级**：指向 `source-docs/`、`meta/` 的 wikilink 会降级成纯文本，既避免满站死链，也不变相公开研报目录。
- **上线前自检**：`scripts/verify_build.py` 会拦下 `.md` 泄漏、本机绝对路径、死链、搜索索引里的 frontmatter 泄漏。
- **不发布源码**：产物只有 HTML/CSS/JS/JSON，不含 vault 的 `.md` 源文，也不含引擎代码。注意：静态站的 JS 本身对访问者始终可见，这是物理事实，不要指望隐藏。

## 前置条件

- Node.js ≥ 22（本仓库用 Quartz v5）
- Python ≥ 3.10（同步与自检脚本，仅用标准库）

## 配置

复制示例并按自己的机器改：

```bash
cp publish.config.example.json publish.config.json
```

`publish.config.json` 含本机 vault 路径，**已被 `.gitignore` 忽略**，不要提交。

vault 解析优先级：`--vault` 参数 → `QUANT_WIKI_VAULT` 环境变量 → 配置文件 `vault` 字段。**都拿不到就直接报错**，不会猜路径。

## 命令

```bash
# 0) 安装依赖（首次）
npm install

# 1) 从 vault 同步 + 脱敏（只读 vault）
python scripts/sync_from_vault.py            # 正式写入 content/
python scripts/sync_from_vault.py --dry-run  # 只报告，不落盘

# 2) 构建
npx quartz build

# 3) 本地预览
npx quartz build --serve --port 8080

# 4) 上线前自检（必跑）
python scripts/verify_build.py
```

`npx quartz build` 产物在 `public/`，是纯静态文件。

## 已知约束（PoC 实测）

- **不要把 `content/` 加进 `.gitignore`**。Quartz 的内容 glob 会尊重 gitignore，一旦忽略 `content/`，构建会直接 `Found 0 input files`。同理，Git 集成部署拿不到 vault，**必须**把生成好的 `content/` 提交进仓库。
- **中文 slug 安全**。Quartz v5 的 `slugifyPath` 只做空格转 `-`、去 `<>:"|*#?`、转小写，**不剥离 CJK**；中文路径原样保留，浏览器自动 percent-encode。已实测 Cloudflare Pages 能把 `/paradigms/凸显权重收益协方差` 正确路由到对应页面。
- **`remove-draft` 只认 `draft: true`**。vault 页面用的是 `stage: draft`，不会被误删。
- **字体要配在插件上**。v5 里 `theme.typography` 与 `@quartz-community/quartz-fonts` 的 options 都影响输出；只删 `theme.typography` 会让 `joinStyles` 读不到 `.title` 而崩。用 `fontOrigin: local` + 完整系统字体栈，避免任何外部字体请求。
- **`alias-redirects` 会按 frontmatter `aliases` 生成根目录重定向页**。vault 里有大量英文别名，开启会多出 200+ 个根目录文件；若内部链接都用 `目录/页名` 路径式，可以直接关掉。
- **`og-image` 需要字体**。默认走 Google Fonts，在无外网或中文场景会退化成 arial → 中文变豆腐块，建议关闭。
- **SPA 站内跳转已实测通过**（clean URL 下 URL 与标题正常切换）。

## 精选子集（抽稀）

`publish.config.json` 的 `curated` 是「要公开的文件相对路径列表」，**路径带 `.md`**。
留空 `[]` → 公开全部；填入列表 → 只公开列表内的页。

当前配置为 **250 页**（原始 395 页）。

### 为什么不能等比例抽

实测四种策略（基于真实链接图，394 页 / 1829 条边，index 单独处理）：

| 策略 | 保留 | 死链 | 死链率 |
|---|---:|---:|---:|
| 随机抽样 | 200 | 714 | 51.8% |
| 按目录等比例 | 200 | 438 | 43.1% |
| 分层固定配额 | 200 | 220 | 23.4% |
| 纯贪心（只求链接完整） | 200 | 77 | 5.7% |
| **配额打底 + 局部搜索** | 250 | **100** | **6.2%** |

关键：**各层价值差异极大**。`data-semantics/量价` 只有 10 页却平均被引用 24.6 次
（`价格` 一页被引 90 次），而 `patterns/fields` 153 页平均仅 4.9 次。

另一个坑：**纯按链接优化会毁掉展示**。贪心方案死链最少，但把 `paradigms`
砍到只剩 7 页、`data-semantics/基本面` 归零 —— 数学最优，展示不可接受。
因此必须加**分层下限**，并允许**锁定**语义重要的页。

### 选型方法

1. 按分层配额选出种子（保证每层都有代表）
2. 交换式爬山：把「被引用多但未收录」的页换进来，把「留着但链接价值低」的换出去
3. 约束：不得跌破分层下限，不得换出锁定页

当前配比：

| 层 | 保留/全量 | 保留率 |
|---|---|---|
| `alpha` | 30/30 | 100% |
| `concepts` | 18/18 | 100% |
| `data-semantics/量价` | 10/10 | 100% |
| `patterns/agg` | 19/23 | 83% |
| `patterns/mask` | 12/15 | 80% |
| `paradigms` | 43/59 | 73% |
| `patterns/fields` | 86/153 | 56% |
| `data-semantics/基本面` | 12/23 | 52% |
| `patterns/ops` | 20/63 | 32% |

保密角度：保留率最低的 `fields` / `ops` 恰是最敏感的 IP 层，
而保留率高的 `alpha` / `concepts` / `量价语义` 是方法论与框架层，展示价值高。

### 三处必须配套的机制

抽稀不只是「少同步几个文件」，还需要：

1. **链接降级扩展到未收录页** —— `redact_wikilinks` 现在除了按目录前缀
   （`source-docs` / `meta`），还会把「解析到未收录页」的链接降级为纯文本。
   否则这些链接会渲染成 Quartz 的 `broken` 样式（`opacity: .5` 的半透明链接），
   看起来像坏掉的页面。
2. **首页重生**（`regenerateIndex: true`）—— 原 `index.md` 链向全部页面，
   抽稀后它自己就会制造大量断链。改为按子集生成、分层分组、标注收录规模。
3. **锁定清单** —— 保证语义重要的页不被算法误删。

### 链接解析注意

Quartz 的链接解析是 `shortest` 策略，`[[凸显权重]]` 也能命中
`patterns/fields/凸显权重`，所以判断「是否收录」时要同时按**全路径**与**文件名**解析。

⚠️ 另一个坑：Markdown 表格里 `[[目标|别名]]` 必须写成 `[[目标\|别名]]`，
正则会把转义产生的**尾部反斜杠**吃进目标里（实测约 310 条），
必须 `rstrip("\\")` 后再解析，否则这些链接会被误判为不可解析。

## 演示版本提示

站点是演示子集（未公开 `source-docs` 等），因此在页面上加提示。文案与开关都在
`publish.config.json` 的 `demoNotice`：

| 键 | 作用 |
|---|---|
| `enabled` | 总开关 |
| `scope` | `all` = 每页加简版；`index` = 只在首页加 |
| `kind` | callout 类型（`info` / `note` / `warning` / `tip` …） |
| `pageTitle` / `pageLines` | 各页简版提示 |
| `indexTitle` / `indexLines` | 首页详版提示（配了就优先生效） |

提示块里带一个 HTML 注释 sentinel（默认 `DEMO-NOTICE`），既保证幂等，也便于事后检索。

## 静态文件（`robots.txt` / `_headers`）

这两个文件**必须写在 `publish.config.json` 的 `staticFiles` 里**，由同步脚本生成。

⚠️ **不要手工往 `content/` 里放静态文件** —— `sync_from_vault.py` 每次会先
`rmtree(content/)` 再重建，手工放进去的文件会被抹掉。曾经因为这个，
`robots.txt` 与 `_headers` 在多次同步后被静默删除，**整个站点的 noindex 保护失效**。
现在自检会检查这两个文件是否在位、内容是否正确。

## 精选子集（续）

## 部署到 Cloudflare Pages

**当前状态**：已部署到 <https://quant-wiki-showcase.pages.dev>（Cloudflare Pages 项目 `quant-wiki-showcase`）。

产出物只有 HTML/CSS/JS/JSON（约 15 MB / 473 文件），直接上传即可：

```bash
npx quartz build
npx wrangler pages deploy public --project-name quant-wiki-showcase --branch main
```

### 更新流程（一页流程）

```bash
python scripts/sync_from_vault.py   # 从 vault 重新同步 + 脱敏
npx quartz build                    # 构建
python scripts/verify_build.py      # 自检（必须通过）
npx wrangler pages deploy public --project-name quant-wiki-showcase --branch main
```

### 关于自动构建

当前是 **wrangler 直接上传**，`git push` **不会**自动触发部署 —— 改完内容要跑上面的更新流程。

如果想要「push 即自动部署」，需要另建一个走 Git 集成的 Pages 项目（连本仓库），配置为：

| 设置项 | 值 |
|---|---|
| Framework preset | `None` |
| Build command | `npx quartz build` ← 不要用 `npm run build`（其 `prebuild` 会跑 `install-plugins`，在 CI 上会失败） |
| Build output directory | `public` |
| 环境变量 | `NODE_VERSION` = `22`（Quartz v5 要求 Node ≥22） |

Git 集成构建能从仓库里的 `content/` 出站，**不需要** vault。

### 已实测的上线行为

- Cloudflare Pages 的 clean-URL 路由能把**无 `.html` 后缀的中文路径**正确路由（`/paradigms/凸显权重收益协方差` → `凸显权重收益协方差.html`），SPA 站内跳转正常。
- **`.md` 源文件访问返回 404** —— 源文没有进入产物，也无法被直接拉取。
- `X-Robots-Tag: noindex, nofollow, noarchive` 响应头生效（来自 `content/_headers`），`robots.txt` 返回 `Disallow: /`。
- 线上页面控制台 0 error / 0 warning。

### 防收录

已配好两道：`content/robots.txt`（`Disallow: /`）+ `content/_headers`（`X-Robots-Tag: noindex`）。两个文件都会随构建落到产物根目录，Cloudflare Pages 会自动读取 `_headers`。

## 已知坑（踩过的）

1. **不要把 `content/` 写进 `.gitignore`**
   Quartz 的内容 glob 会尊重 `.gitignore`。一旦忽略 `content/`，构建会直接报
   `Found 0 input files from content`，而不是报错说找不到内容。
2. **`theme.typography` 不能删**
   `joinStyles` 会读 `theme.typography.title`，删掉整段会以
   `Cannot read properties of undefined (reading 'title')` 崩在 emit 阶段。
3. **字体要配在插件上**
   v5 里字体走 `@quartz-community/quartz-fonts` 的 `options`（`header`/`body`/`code`），
   写在 `theme.typography` 里不会被该插件采用。配 `fontOrigin: local` 可完全不请求外部字体。
4. **`alias-redirects` 会按 frontmatter `aliases` 生成大量根目录重定向页**
   （本 vault 约 276 个）。若正文的 wikilink 都用 `目录/页名` 路径式，建议关掉。
5. **`og-image` 是构建耗时大头，且中文会因缺 CJK 字体渲染成豆腐块**
   （日志里的 `Using system font "arial" as fallback`）。无需求建议关闭。
6. **`remove-draft` 只认 `draft: true`**
   本 vault 用的是 `stage: draft`，不会被误删。

## 目录

```
publish.config.json         本机配置（已 gitignore）：vault 路径与发布范围
publish.config.example.json 示例配置
content/                    同步生成的待发布内容（可重建，已 gitignore）
public/                     构建产物（已 gitignore）
quartz.config.yaml          站点配置：标题、主题配色、插件开关
scripts/sync_from_vault.py  同步 + 脱敏
scripts/verify_build.py     上线前自检
quartz/                     Quartz 引擎（上游代码）
```
