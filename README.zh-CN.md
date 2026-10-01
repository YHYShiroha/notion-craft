# notion-craft

[English](README.md) | **简体中文**

一个 [DSH](https://github.com/deepseek-ai) 技能：让 agent 通过 Notion 官方 API 读写 Notion 内容 —— 查找页面、读取正文、查询数据库与数据源、创建和更新页面、追加与编辑块、发表评论。**删除默认只做干跑**，必须经过两道显式确认才会真正执行。

只用 Python 标准库。无第三方依赖、不需要 MCP 服务、不做浏览器自动化。

```
                                 ┌─────────────────────────┐
   你的指令    ──►   agent  ──►  │  scripts/notion.py      │  ──►  api.notion.com
                                 │  （+ 删除闸门、          │
                                 │     + 审计日志）         │
                                 └─────────────────────────┘
```

---

## 目录

- [为什么需要这个技能](#为什么需要这个技能)
- [环境要求](#环境要求)
- [安装](#安装)
- [获取 Notion Token 并存放](#获取-notion-token-并存放)
  - [第 1 步 —— 创建内部集成](#第-1-步--创建内部集成)
  - [第 2 步 —— 配置能力权限](#第-2-步--配置能力权限)
  - [第 3 步 —— 复制密钥](#第-3-步--复制密钥)
  - [第 4 步 —— 把页面共享给集成](#第-4-步--把页面共享给集成)
  - [第 5 步 —— 把 token 放到技能能找到的位置](#第-5-步--把-token-放到技能能找到的位置)
- [验证配置](#验证配置)
- [使用示例](#使用示例)
- [命令参考](#命令参考)
- [删除防护机制](#删除防护机制)
- [API 版本与数据源](#api-版本与数据源)
- [故障排查](#故障排查)
- [开发](#开发)
- [安全](#安全)
- [许可证](#许可证)

---

## 为什么需要这个技能

市面上大多数「Notion 自动化」都会在三个地方翻车，这个技能就是围绕它们设计的：

1. **误删。** 一个能写的 agent 同样能毁。这里的 `page delete`、`block delete`、`ds delete-page` **只报告不执行**，除非你同时传入 `--yes` 和 `--confirm <精确 ID 或标题>`。删除是移入 Notion 回收站，你可以恢复。**代码里根本不存在永久删除的路径** —— 因为 Notion 没有给集成提供这个接口，这个技能也不会假装有。
2. **2025 年的数据源拆分。** 从 API 版本 `2025-09-03` 起，数据库变成了**容器**，下面挂一个或多个**数据源**，而只有数据源才能被查询。一个粗糙的集成会在数据库出现第二个数据源时立刻失效。本技能会自动解析数据源，并对旧版本 API 做兼容回退。
3. **每个工作区属性名都不一样。** 标题属性在这个库里叫 `Name`，在那个库里叫 `title`，在你的库里叫 `标题`。`--title` 会先读父级 schema，再把自己映射到真实属性名，而不是写死一个。

技能的所有写操作（**包括干跑**）都会追加到技能目录旁的审计日志，任何时候都有据可查。

---

## 环境要求

| | |
|---|---|
| Python | 3.9 或更高（Windows 上 `python` 或 `py -3`）。仅用标准库。 |
| 网络 | 能 HTTPS 访问 `api.notion.com` |
| Notion | 一个你可以创建集成的工作区 |
| DSH | 任意较新版本；技能从文件系统技能目录中被发现 |

---

## 安装

把这个目录复制（或克隆）到 DSH 会扫描的技能目录下。用户级目录对所有会话都生效：

```bash
# Linux / macOS
cp -r notion-craft ~/.dsh/skills/notion-craft

# Windows (PowerShell)
Copy-Item -Recurse notion-craft "$env:USERPROFILE\.dsh\skills\notion-craft"
```

扫描的根目录，按优先级从高到低：

| 优先级 | 来源 | 路径 |
|---|---|---|
| 100 | 项目 | `<项目>/.dsh/skills` |
| 200 | 项目 | `<项目>/.agents/skills` |
| 400 | 用户 | `~/.dsh/skills` |
| 500 | 用户 | `~/.agents/skills` |

DSH 会监听这些目录，所以**无需重启应用**技能即可出现。让 agent 使用 `notion-craft` 即可确认。

---

## 获取 Notion Token 并存放

本技能使用 **Notion 内部集成 token**（Internal Integration Token）鉴权。你自己在 Notion 里创建，大约两分钟。

### 第 1 步 —— 创建内部集成

1. 登录 Notion，打开 <https://www.notion.so/my-integrations>。
2. 点击 **New integration**（新建集成）。
3. 填写：
   - **Name（名称）** —— 随便取，例如 `notion-craft`。这个名字之后会出现在 Notion 的「连接 / Connections」列表里，所以取个好认的。
   - **Associated workspace（关联工作区）** —— **选你要操作的那个工作区**。选错了，后面所有页面都会「看起来不存在」。
   - **Type（类型）** —— 必须选 **Internal**（内部）。本技能不支持 Public/OAuth 集成。
4. 点击 **Save**（保存）。

### 第 2 步 —— 配置能力权限

在集成页面打开 **Capabilities（能力）**，按需选择：

| 能力 | 用于 | 说明 |
|---|---|---|
| **Read content**（读取内容） | 一切操作 | 必需 |
| **Update content**（更新内容） | `page update`、`block update` | 编辑必需 |
| **Insert content**（插入内容） | `page create`、`page append`、`ds create-page`、评论 | 新建内容必需 |
| **Delete content**（删除内容） | `page delete`、`block delete`、`ds delete-page` | 可选 —— **想彻底禁止删除就把它关掉** |

把 **Delete content** 关掉后，删除类命令会返回 `403`。这是在 CLI 自身确认流程之外，**由服务端提供的第二道闸门**。

### 第 3 步 —— 复制密钥

回到集成的 **Configuration（配置）** 页，找到 **Internal Integration Secret**，点击 **Show**，再点 **Copy**。格式是 `ntn_…`（较新）或 `secret_…`（较旧）。

> 这个值**只显示一次**。丢了就只能重新生成。永远不要提交进仓库、不要贴进 Notion 页面、不要放进 URL。

### 第 4 步 —— 把页面共享给集成

**光有 token 什么都访问不到。** Notion 集成初始可见范围是空的，你必须把集成连接到你想让它看到的内容上：

1. 在 Notion 里打开目标**页面或数据库**。
2. 点击右上角 **`...`** → **Connections（连接）** → **Connect to（连接到）** → 选中你的集成。
3. 数据库要**在数据库本身上再连一次** —— 共享父页面**不会**自动覆盖数据库里的行。

Notion 把集成能看到的范围限定在你连接的页面上。漏做这一步的典型症状：`whoami` 成功，但 `search` 返回空、具体页面返回 `404`。

### 第 5 步 —— 把 token 放到技能能找到的位置

CLI 按以下顺序解析凭据，**先命中者优先**：

1. 命令上的 `--token <值>`
2. 环境变量 —— `NOTION_TOKEN`、`NOTION_API_KEY` 或 `NOTION_INTEGRATION_TOKEN`
3. 凭据文件：**`~/.dsh/notion-credentials.json`**

**推荐做法（文件）：**

```bash
python <skill-dir>/scripts/notion_auth.py set-token ntn_your_token_here
```

这条命令会写入 `~/.dsh/notion-credentials.json`，在操作系统支持时把它限制为仅当前用户可读，并立即验证 token。在 Windows 上文件位于：

```
C:\Users\<你的用户名>\.dsh\notion-credentials.json
```

文件就是普通 JSON —— 完整结构如下：

```json
{
  "token": "ntn_your_token_here",
  "saved_at": "2026-01-01T00:00:00+00:00"
}
```

> 凭据文件**刻意放在本仓库之外**。本仓库的 `.gitignore` 同时也忽略了 `*credentials*.json`、`.env`、`*.token` 和审计日志，作为第二道防线；一旦密钥真的落进仓库树，`scripts/check_no_secrets.py` 会直接报错失败。

**替代做法（环境变量）：**

```bash
# Linux / macOS
export NOTION_TOKEN=ntn_your_token_here

# Windows PowerShell，仅当前会话
$env:NOTION_TOKEN = 'ntn_your_token_here'

# Windows，为当前用户永久设置
setx NOTION_TOKEN "ntn_your_token_here"
```

想知道当前用的是哪个来源，或删除已存的 token：

```bash
python <skill-dir>/scripts/notion_auth.py where       # 当前生效的凭据来源
python <skill-dir>/scripts/notion_auth.py clear       # 干跑
python <skill-dir>/scripts/notion_auth.py clear --yes # 真正删除文件
```

---

## 验证配置

```bash
python <skill-dir>/scripts/notion_auth.py check     # token 有效吗？
python <skill-dir>/scripts/notion.py whoami         # bot 名称 + 工作区
python <skill-dir>/scripts/notion.py search --limit 5
```

预期结果：`check` 打印出你的集成和工作区；`search` 列出你已共享的对象。
如果 `search` 返回 `count: 0`，回到[第 4 步](#第-4-步--把页面共享给集成)。

---

## 使用示例

ID 可以是裸 32 位十六进制、带连字符的 UUID，或完整的 Notion 链接 —— 三种在任何需要 ID 的地方都能用。所有命令默认输出 JSON；有 Markdown 视图的地方可加 `--format md`。

**查找**

```bash
python <skill-dir>/scripts/notion.py search "季度计划" --limit 10
python <skill-dir>/scripts/notion.py search --type page
```

**读取**

```bash
python <skill-dir>/scripts/notion.py page get <页面-ID-或链接>
python <skill-dir>/scripts/notion.py page children <页面-ID> --depth 2 --format md
```

**创建页面**

```bash
python <skill-dir>/scripts/notion.py page create \
  --parent-type page --parent-id <页面-ID> \
  --title "会议记录" --markdown "## 议程
- 第一项
- [ ] 待跟进"
```

**在数据库中新建一行**

```bash
python <skill-dir>/scripts/notion.py ds create-page <数据源-ID> \
  --title "新任务" --prop "Status:select=Todo" --prop "Due:date=2026-03-01"
```

**更新属性**

```bash
python <skill-dir>/scripts/notion.py page update <页面-ID> --prop "Status:select=Done"
```

**查询数据库**

```bash
python <skill-dir>/scripts/notion.py db query <数据库-ID> --limit 20 \
  --filter '{"property":"Status","select":{"equals":"Todo"}}' \
  --sort '{"property":"Due","direction":"ascending"}'
```

**追加与编辑块**

```bash
python <skill-dir>/scripts/notion.py page append <页面-ID> --text "每行一个块"
python <skill-dir>/scripts/notion.py block update <块-ID> --text "修正后的文本"
```

**删除 —— 永远先干跑**

```bash
python <skill-dir>/scripts/notion.py page delete <页面-ID>          # 只报告，不执行
python <skill-dir>/scripts/notion.py page delete <页面-ID> --yes --confirm <ID-或-标题>
```

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `whoami` | 验证 token，打印集成身份 |
| `search [关键词] [--type page\|data_source\|database] [--limit N]` | 查找已共享的页面与数据源 |
| `users [--limit N]` | 列出集成可见的工作区用户 |
| `page get <ID> [--no-properties]` | 以纯文本形式返回页面属性 |
| `page children <ID> [--depth N] [--limit N] [--format md]` | 页面正文的块结构 + Markdown |
| `page create --parent-type page\|database\|data_source\|workspace --parent-id <ID> --title T [--prop P=V] [--markdown M\|--text T\|--blocks-json J] [--icon E] [--cover URL]` | 创建页面或数据行 |
| `page update <ID> [--prop P=V] [--title T] [--icon E] [--cover URL] [--trash --yes --confirm C]` | 更新属性（可选移入回收站） |
| `page append <ID> [--markdown M\|--text T\|--blocks-json J]` | 追加块 |
| `page delete <ID> [--yes --confirm C] [--purge]` | 把页面移入回收站（默认干跑） |
| `block get <ID>` | 获取单个块 |
| `block children <ID> [--limit N]` | 获取块的子块 |
| `block append <ID> [...]` | 给块追加子块 |
| `block update <ID> --text T` | 替换块的文本 |
| `block delete <ID> [--yes --confirm C]` | 删除块（默认干跑） |
| `db get <ID>` / `db sources <ID>` | 数据库容器及其数据源 |
| `db query <ID> [--filter J] [--sort J] [--limit N] [--page-size N]` | 查询数据行，自动解析数据源 |
| `db create <父页面-ID> --title T --properties-json J` | 创建数据库 |
| `ds get <ID>` / `ds query <ID> [...]` | 数据源操作 |
| `ds create-page <ID> --title T [--prop P=V]` | 在数据源中创建一行 |
| `ds delete-page <ID> [--yes --confirm C]` | 把一行移入回收站（默认干跑） |
| `comment list <ID>` / `comment add <页面-ID> --text T` | 读取与发表评论 |
| `ref <链接或-ID>` | 在 Notion 链接与 ID 之间转换（离线，无需 token） |

全局参数：`--token`、`--api-version`、`--format json|md`、`--verbose`。放在子命令**前后都可以**。

`--prop` 接受 `Name=Value`（默认按富文本处理）或 `Name:type=Value`，类型支持 `title`、`rich_text`、`number`、`checkbox`、`select`、`multi_select`、`date`、`url`、`email`、`relation`、`people`。也可以直接给原始 JSON：`--prop 'Tags=[{"name":"api"}]'`。
`--filter`、`--sort`、`--blocks-json` 接受内联 JSON、`@file.json`，或 `-` 从标准输入读取。

---

## 删除防护机制

删除是唯一可能造成数据丢失的操作，所以它被五重围栏保护：

1. **默认干跑。** 不带 `--yes` 时，命令会从 Notion 拉取目标对象的**真实标题**和 ID 并打印出来，然后退出，**不发送任何写请求**。输出中 `"dry_run": true`，此外什么都没发生。
2. **只有 `--yes` 会被拒绝。** 你还必须提供 `--confirm <精确 ID、ID 前缀或精确标题>`，至少 4 个字符。不匹配就拒绝执行，并列出候选目标，保证不会因为打错一个字而删错行。
3. **进回收站，而不是彻底清除。** 删除发送的是 `in_trash: true`，你可以从 Notion 回收站恢复。`--purge` 被直接拒绝 —— 因为 Notion 没有为集成提供永久删除页面/块的接口。
4. **批量删除受限。** 一次超过一个目标就必须提供 `--max <n>`，实际匹配数不得超过它，且单次调用的硬上限是 25 个对象。
5. **全部留痕。** 每一次写操作（含干跑）都会在技能目录旁的 `.audit.log` 追加一行 JSON。该日志已被 `.gitignore` 忽略，且测试永远不会污染它（测试会把它重定向到空设备）。

实际效果演示：

```console
$ python scripts/notion.py page delete 3ecee4421d4b81ad964ad52d95cdb9aa
{
  "dry_run": true,
  "action": "trash",
  "target": { "short_id": "3ecee442", "title": "旧草稿", "type": "page" },
  "hint": "nothing was deleted. Re-run with --yes --confirm 3ecee442 to proceed."
}

$ python scripts/notion.py page delete 3ecee4421d4b81ad964ad52d95cdb9aa --yes --confirm 3ecee442
{ "dry_run": false, "action": "trashed", "in_trash": true, ... }
```

技能的 `SKILL.md` 里还额外约束 agent：**除非用户在本次对话中明确要求删除某个具体对象，否则绝不删除任何东西。**

---

## API 版本与数据源

| | `2022-06-28` | `2025-09-03`（默认） |
|---|---|---|
| 数据库 | 直接持有数据行 | **容器**，拥有一个或多个数据源 |
| 查询数据行 | `/v1/databases/{id}/query` | `/v1/data_sources/{id}/query` |
| 数据行的页面父级 | `database_id` | `data_source_id` |
| 搜索过滤值 | `database` | `data_source` |
| 归档字段 | `archived` | `in_trash` |

`db query` 已经处理了这些差异：它会把数据库 ID 解析到第一个数据源；如果旧版本 API 返回 `multiple_data_sources_for_database`，它会自动改走 `/v1/data_sources` 重试。只有在你的工作区早于数据源模型时才需要 `--api-version 2022-06-28`。更多细节见 [`references/notion-api.md`](references/notion-api.md)。

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `no Notion token found` | 所有来源都没有 token。执行 `notion_auth.py set-token`，或设置 `NOTION_TOKEN`。 |
| `401 unauthorized` | token 错误、已吊销，或来自已删除的集成。重新生成密钥。 |
| `403 restricted_resource` | 某项能力被关闭（例如 **Delete content**），或对象未共享。 |
| `404 object_not_found` | 页面/数据库从未连接过集成 —— 见第 4 步。 |
| `search` 返回 `count: 0` | 还没有任何内容共享给集成。 |
| `429 rate_limited` | 限流约 3 请求/秒。CLI 会自动节流重试；降低 `--limit`。 |
| 属性报 `validation_error` | 属性名或类型不对。用 `db get`/`ds get` 查 schema；注意标题属性名每个库都不同。 |
| `Databases with multiple data sources are not supported` | 你在多数据源数据库上强制使用了 `--api-version 2022-06-28`。去掉这个参数。 |
| 技能没有出现 | 目录必须直接位于某个被扫描的根目录下，且包含 `SKILL.md`。检查目录名与 frontmatter 里的 `name:` 是否一致。 |

---

## 开发

无依赖，不需要虚拟环境：

```bash
python scripts/test_notion_skill.py     # 41 项离线测试：不联网、不需要 token
python scripts/check_no_secrets.py      # 一旦密钥泄漏进仓库树就失败
```

`test_notion_skill.py` 用桩替换了 HTTP 层，因此可以安全地在 CI 中运行。它覆盖删除闸门、ID 解析、分页、旧版本数据源回退、属性与 Markdown 解析、错误映射，以及参数解析器。请保持全绿。

`check_no_secrets.py` **只报告文件名和命中数量，绝不回显密钥本体**，所以它的输出可以安全地贴进日志。

目录结构：

```
notion-craft/
├── SKILL.md                    # agent 指令：删除红线、工作流、注意事项
├── README.md                   # 英文文档
├── README.zh-CN.md             # 中文文档（本文件）
├── references/notion-api.md    # 端点、请求体结构、版本差异、错误码
└── scripts/
    ├── notion.py               # CLI：25 个子命令，基于 argparse
    ├── notion_client.py        # API 客户端、分页、删除闸门、审计日志
    ├── notion_auth.py          # 凭据的 set/check/where/clear
    ├── test_notion_skill.py    # 离线测试套件
    └── check_no_secrets.py     # 密钥泄漏扫描
```

---

## 安全

- token 来自 `--token`、环境变量或 `~/.dsh/notion-credentials.json` —— **绝不来自本仓库**。
- 本仓库中没有任何地方会打印完整 token。`notion_auth.py` 会做掩码（`ntn_…hmn5`），泄漏扫描器也从不回显命中内容。
- 发布或提交前请运行 `scripts/check_no_secrets.py`。
- 一旦密钥进入过提交，**立刻轮换**：Notion 集成支持重新生成密钥，而重写 GitHub 历史**不能替代轮换**。
- 权限范围由 Notion 侧决定：集成只能访问用户显式连接过的页面；你也可以关闭 **Delete content**，从 API 层面让删除变得不可能。

---

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
