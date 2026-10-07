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

## 精选子集

`publish.config.json` 的 `curated` 是「要公开的文件相对路径列表」。

- 留空 `[]` → 公开**全部**（除 `excludeDirs` / `excludeFiles`）。
- 填入列表 → **只**公开列表内的页面。

简历展示建议只放 60–100 页的精选子集，而非全量。

## 部署到 Cloudflare Pages

产物是纯静态，直接上传即可：

```bash
npx wrangler pages deploy public --project-name <你的项目名>
```

也可以用 Pages 的 Git 集成（连本仓库，构建命令 `npx quartz build`，输出目录 `public`）。

**已本地验证**：Cloudflare Pages 的 clean-URL 路由能把**无 `.html` 后缀的中文路径**正确路由到对应页面（`/paradigms/凸显权重收益协方差` → `凸显权重收益协方差.html`），SPA 站内跳转也正常。

### 防收录

投简历用的站点建议关掉搜索引擎收录，在 `content/` 里放一个 `robots.txt` 并在站点配置里加 `noindex`，同时不要把链接公开传播。

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
