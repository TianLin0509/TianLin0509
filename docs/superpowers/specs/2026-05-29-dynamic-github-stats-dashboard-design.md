# 动态 GitHub 统计仪表盘 — 设计文档

- **日期**：2026-05-29
- **仓库**：`TianLin0509/TianLin0509`（GitHub profile 仓库，本地 `C:\Users\lintian\TianLin0509`）
- **目标**：把当前手工写死的 `assets/github-stats-dashboard.svg`（数据定格 2026-05-24）改造为**每天自动拉取实时数据并重渲染**的动态仪表盘，并在保留原版式基础上**新增贡献热力图、连续提交 streak、近 30 天提交趋势**三块内容。

## 1. 背景与现状

- 仓库当前只有 `README.md`（一行 `<img>` 引用 SVG）+ `assets/github-stats-dashboard.svg`（121 行纯手工 SVG，所有数字硬编码）。
- 无生成脚本、无 GitHub Actions。这是一张"截图"，永不更新。
- 现有 SVG 含 6 个区块：6 个 KPI 卡 / Star 排行 / Commit 排行 / 语言分布环图 / 语言贡献 / 最近活跃仓库时间线。画布 980×920，GitHub 暗色调（bg `#0d1117`，panel `#161b22`，文字 `#e6edf3`）。

## 2. 范围（用户已确认 = B 方案 + A 数据源）

- **动态化**：实时数据替换硬编码，外观保持现有版式。
- **增强**：新增热力图 + streak + 近 30 天趋势（核心数据均来自贡献日历）。
- **数据源决策**：使用用户的经典 **PAT**（`read:user` 权限，存为仓库 Secret `GH_PAT`），保证贡献日历的真实完整（含私有贡献、PR、issue）。
- **不在范围**：不换成 github-readme-stats / metrics 等现成方案；不增加额外小数字指标（用户未要求）。

## 3. 架构

零服务器，全部依赖 GitHub 免费能力：

```
.github/workflows/update-dashboard.yml   # 每天定时 + 手动触发；跑脚本、有变化则 commit
scripts/generate_dashboard.py            # 生成器：拉数据 → 算指标 → 渲染 SVG
assets/github-stats-dashboard.svg        # 脚本产物（覆盖现有手工版）
README.md                                # 不改动（已在引用该 SVG）
docs/superpowers/specs/...               # 本设计文档
```

数据流：`cron 触发 → Actions 运行 generate_dashboard.py（注入 GH_PAT + GITHUB_TOKEN）→ 写出 SVG → git diff 有变化则 commit & push → GitHub README 渲染最新图`。

## 4. 生成器脚本 `scripts/generate_dashboard.py`

### 4.1 技术约束
- **仅用 Python 标准库**（`urllib.request`、`json`、`datetime`、`math`），不装任何 pip 包 → 无依赖安装、CI 跑得快、长期不腐。
- 用户名可配置常量（默认 `TianLin0509`）。
- 从环境变量读 `GH_PAT`（GraphQL 必需）；REST 调用同样带上该 token 以提高额度。
- **确定性输出**：所有列表显式排序、数字格式固定、时间戳精确到日期 → 数据未变时 SVG 字节一致，不产生噪音 commit。

### 4.2 数据获取

**REST API（`https://api.github.com`）**
- `GET /users/{user}` → `public_repos`、`followers`。
- `GET /users/{user}/repos?per_page=100&type=owner`（分页直到取完）→ 每仓库：`name`、`stargazers_count`、`forks_count`、`language`、`pushed_at`、`default_branch`。
- 每仓库默认分支 commit 数：`GET /repos/{user}/{repo}/commits?sha={default_branch}&per_page=1`，解析 `Link` 头 `rel="last"` 的页号即 commit 总数；无 Link 头则该仓库 commit 数 = 返回数组长度（0 或 1）。

**GraphQL API（`https://api.github.com/graphql`，需 PAT）**
- 查询 `user(login).contributionsCollection.contributionCalendar`：`totalContributions` + `weeks[].contributionDays[]{date, contributionCount, weekday, color}`。一次拿全，供热力图 / streak / 趋势复用。

### 4.3 指标计算
- **6 个 KPI**：公开仓库数 / Total Stars（`stargazers_count` 求和）/ 默认分支 Commits（各仓库 commit 数求和）/ Forks（`forks_count` 求和）/ Followers / 近 365 天活跃仓库（`pushed_at` 在 365 天内的仓库数）。口径与现有图一致。
- **Star 排行**：按 star 降序取前 6，条宽 = `round(value / max_value * 236)`。
- **Commit 排行**：按 commit 数降序取前 6，同样缩放到 236。
- **语言分布**：按主语言（`language`，`null` 归为「未标注」）统计仓库数，环图分段**按比例占满整圈**（修正原图未占满的手工误差），图例取前 5。
- **语言贡献**：按语言聚合 star 与 commit，取前 4，每行两条（star 琥珀色、commit 蓝色），按各自最大值缩放并 clamp 到面板宽度（修正原图 JS 柱错位）。
- **最近活跃仓库**：按 `pushed_at` 降序取前 8，时间线圆点 + 短名（名称过长时截断）。
- **贡献热力图**：53 周 × 7 天网格，每格按 `contributionCount` 映射 GitHub 官方 5 档绿阶（0=`#161b22`，依次 `#0e4429`/`#006d32`/`#26a641`/`#39d353`）；附「X contributions in the last year」与 Less→More 图例。
- **连续提交 streak**：由日历倒序计算——当前 streak（从今天或昨天起连续 `count>0` 的天数）+ 最长 streak（全期最长连续段）。
- **近 30 天趋势**：取日历最后 30 天 `contributionCount`，渲染迷你柱图（高度按当期最大值归一化）。

### 4.4 渲染
- 纯字符串模板拼接 SVG（与现有风格、class、配色一致）。
- 画布由 980×920 加高至约 980×1240，容纳两块新内容。
- 版面顺序：标题 → 6 KPI → Star/Commit 排行 → 语言分布/语言贡献 → **贡献热力图（全宽，新）** → **连续提交 + 近 30 天趋势（左右分栏，新）** → 最近活跃仓库（下移）→ 口径脚注。
- 时间戳显示「更新于 YYYY-MM-DD」（Asia/Shanghai = UTC+8），精确到日。

### 4.5 错误处理（铁律：禁止静默失败）
- 任一 API 请求失败、GraphQL 返回错误、或日历为空 → **打印错误并以非零码退出**，不写出空/零数据的坏 SVG。
- 失败时 Actions 显示红叉、旧 SVG 保留不动。

## 5. 工作流 `.github/workflows/update-dashboard.yml`

- 触发：`schedule`（cron 每天一次，UTC 时间换算到合适点）+ `workflow_dispatch`（手动）。
- `permissions: contents: write`。
- 步骤：`checkout` → `setup-python` → 运行脚本（env 注入 `GH_PAT`（secret）与 `GITHUB_TOKEN`）→ `git add assets/...` → 若 `git diff --cached --quiet` 为假则用 bot 身份 commit & push（commit message 如 `chore: refresh github stats dashboard`）。
- 数据无变化（含日期未变）时跳过 commit。

## 6. 用户需完成的一次性配置

1. GitHub → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → 生成新 token，勾选 `read:user`（如需统计私有仓库 commit 数另需 `repo`，但本设计 KPI 口径只用公开数据，`read:user` 足够热力图）。
2. 仓库 `TianLin0509/TianLin0509` → Settings → Secrets and variables → Actions → New repository secret，名称 `GH_PAT`，值为上面的 token。
   （实现阶段会给出逐步截图级说明。）

## 7. 验证策略（铁律：测试必须真实执行）

- **公开数据部分**：本地实跑脚本（不带 PAT 也能拉 REST 公开数据，单次调用量 < 60 次/小时未认证上限），核对 KPI 与现有图对得上（32 / 1,306 / 1,784 / 440 / 512 / 12 量级一致）。
- **热力图/streak/趋势**：依赖 PAT，本地先用 mock 日历数据渲染验证版面，再由用户加好 `GH_PAT` Secret 后**手动触发一次真实 Action 跑绿**验收真实数据。此环节如实汇报，不假装测过。

## 8. 已定默认值
- 更新频率：每天 1 次 + 手动触发。
- 时间戳：精确到日期。
- 失败即报红退出，不写坏数据。

## 9. 风险与取舍
- **camo 缓存**：README 引用的图经 GitHub 图片代理缓存，数据更新后可能延迟数分钟至数小时才显示，可接受（每日更新）。
- **环图/语言贡献修正**：新版按数学比例渲染，与旧手工图会有像素级差异，属预期改进。
- **PAT 维护**：经典 token 若设了有效期，到期需用户重建并更新 Secret（实现说明里提示）。
