# RecallFlow

[简体中文](#简体中文介绍) · [English](#product-case-study)

## 简体中文介绍

**收藏之后，也能在合适的时候重新遇见。**

我们常常保存有用的文章，却很少再回去看。RecallFlow 是一个帮助你回访已收藏知识的小工具：保存文章链接，设定一个回访日期，到期后回到应用，就能看到文章卡片，再决定现在打开、过几天再看，还是不再提醒。

当前 V1 使用透明、可预测的规则，围绕 **Open / Later / Dismiss** 完成核心流程。先建立一个确定性的基础版本，再评估 AI 是否能带来额外价值。

### 使用入口在哪？

当前提供的是可在本地运行的源码版本，尚无在线演示站点。按下面的步骤启动后，在浏览器打开 **[http://127.0.0.1:8000](http://127.0.0.1:8000)** 即可进入。界面按钮目前为英文，下方附有中文说明。

### 本地快速开始（Windows PowerShell）

先准备好 Python 3.11 或更新版本和 Git，在准备存放项目的目录打开 PowerShell：

```powershell
git clone https://github.com/AndyLamyinwik/RecallFlow.git
cd RecallFlow
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

用文本编辑器打开 `.env`，设置两个值：

- `APP_SECRET`：自行生成的随机密钥，建议至少 32 个字符。
- `ACCESS_CODES`：自行生成的随机邀请码，长度为 16–256 个字符；登录时输入这个邀请码。

可用下面的命令生成一个随机值，执行两次，分别用于密钥和邀请码。保留其余默认配置即可；`.env` 和邀请码请只保存在本地。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

然后启动应用：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

保持这个终端运行，在浏览器打开上面的本地入口。完整的配置和安全说明见下方 [Local setup](#local-setup)。

### 怎么使用？

1. **进入应用**：在首页输入 `.env` 中设置的 `ACCESS_CODES` 邀请码，点击 **Enter RecallFlow**。
2. **保存文章**：进入 **Save an article**（`/items/new`），粘贴公开文章链接，选择保存用途和回访日期，也可以写一句收藏原因，点击 **Save article**。
3. **到期回来查看**：打开 **Saved items**（`/items`），查看已保存文章和到期卡片。每个本地自然日最多选出一篇到期文章；需要回到这个页面查看，目前没有邮件或消息推送。
4. **决定下一步**：在卡片上选择下面的操作。

| 界面操作 | 含义 |
| --- | --- |
| **Open** | 检查原文链接可达后跳转到原文；这不代表已经读完。 |
| **Later** | 延后 3 天、7 天，或选择一个未来日期。 |
| **Dismiss** | 不再让这篇文章浮现，文章仍保留在列表中；30 秒内可点击 **Undo Dismiss** 撤销。 |
| **Pause / Resume resurfacing** | 暂停或恢复文章浮现，保留已保存文章和原回访日期。 |

点击列表中的文章可进入详情页。未被 Dismiss 的文章可以修改回访日期；若卡片正在等待处理，日期会暂时锁定。详情页也支持永久删除文章。

### 产品文档

- [用户研究与问题发现](docs/user-research.md)
- [产品需求（PRD）](docs/PRD.md)
- [用户流程](docs/user-flow.md)
- [系统架构](docs/architecture.md)

---

> A rule-based saved-content resurfacing workflow for helping people revisit useful knowledge later.

## Product case study

**Problem.** People save useful knowledge but often fail to reconnect with it later.

**Product.** RecallFlow resurfaces previously saved knowledge on a transparent future date, without presenting an overdue queue or pressuring the user to clear a backlog.

**Current V1.** The non-AI release establishes a deterministic, rule-based baseline built around three clear decisions: **Open**, **Later**, and **Dismiss**.

**Why non-AI first.** V1 deliberately establishes a deterministic baseline before testing whether AI creates incremental value. This keeps the core workflow explainable and gives future experiments a clear comparison point.

This repository is a closed-pilot V1. Its product rules and extraction thresholds are provisional; it does not claim validated retention, learning, or AI results.

## Product Documentation

- [User research](docs/user-research.md)
- [Product requirements](docs/PRD.md)
- [User flow](docs/user-flow.md)
- [Architecture](docs/architecture.md)

## What the current V1 does

- Uses an invitation code and signed session for closed-pilot access.
- Saves public article URLs after SSRF-aware URL and network checks.
- Extracts a title and article text when possible, estimates reading effort with a deterministic `v1` rule, and falls back gracefully when extraction is unavailable.
- Shows a private saved-item list and detail page, with revisit-date editing and permanent deletion.
- Selects at most one due article per user-local day using a deterministic ordering rule. An impression is recorded only after the browser confirms that the card mounted.
- Supports **Open**, **Later**, and **Dismiss** with idempotent state transitions.
- Offers **Undo** for 30 seconds after Dismiss. A valid same-user Undo restores the item and its retained schedule; when the original card context is still current, it restores that card without creating a second impression.
- Supports a user-wide **Pause resurfacing** control. Pause hides/closes any active card but preserves every saved item and revisit date. Resume clears the pause without rewriting dates or creating a duplicate card for an already-used daily slot.
- Records the non-AI product events needed to audit the primary flow.

`source_opened` has a deliberately narrow meaning: RecallFlow completed a fresh safe-reachability check and issued an external navigation handoff. It does **not** mean the browser rendered the article or that the user read it.

## Not implemented

The current V1 does **not** include:

- AI Value Line or any model call
- Recall generation or a Recall interaction
- deployment configuration or a hosted release
- claims that the product hypotheses or `v1` thresholds have been validated

Future AI and Recall design remains in the product and architecture documentation only; those planned sections are not current product functionality.

## Local setup

Create and activate a virtual environment, then install the dependencies:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env`, then configure:

- `APP_SECRET` — a long random secret; use at least 32 characters outside development.
- `ACCESS_CODES` — one or more comma-separated, high-entropy pilot invitation codes (16–256 characters each).
- `DATABASE_URL` — defaults to the local SQLite file `data/recallflow.db`.
- `DISMISS_UNDO_WINDOW_SECONDS` — keep the V1 value at `30`.

An invitation code is converted to a salted scrypt hash when it is first used. The raw code is never stored in the database or session cookie. Never commit `.env`, invitation codes, secrets, or the local SQLite database; the supplied `.gitignore` excludes them.

## Run locally

```bash
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. The health endpoint is `http://127.0.0.1:8000/health`.

Authenticated pages:

- `/items` — pause/resume control, the single due card when available, and saved items
- `/items/new` — public article capture
- `/items/{id}` — private item details, revisit-date control, and permanent deletion

## Run tests

```bash
python -m pytest
```

## Security and privacy boundaries

- Capture accepts public HTTP(S) article targets only and applies address, redirect, response-type, size, and timeout limits.
- A card's Open action performs a fresh bounded reachability check, including public-address, redirect-hop, connected-peer, and cross-user cookie-isolation safeguards, before navigation handoff.
- Adequate, untruncated extracted article text may be stored privately up to 50,000 characters. Longer or inadequate extractions retain only the limited fallback data defined by the V1 extractor. Stored source text is not rendered publicly or committed to this repository.
- Permanent item deletion removes its URL, private source snapshot, note, and item-linked records.
- State-changing browser requests use CSRF protection and ownership checks; resurfacing decisions also use idempotency keys. The 30-second Dismiss Undo reference is signed, same-user scoped, and kept in a short-lived HttpOnly cookie rather than a URL.
- Production/staging mode enables secure-cookie settings, but this repository does not yet provide deployment configuration.

## Current limitations

- The default HTTP transport validates the connected peer after connection rather than pinning the connection to the prevalidated DNS address. Strict DNS-rebinding prevention remains deployment hardening work.
- Closed-pilot invitation codes are intentionally a narrow access boundary, not a broad-release authentication system.
- SQLite is intentionally limited to a single application instance.
- There is no AI, Recall, evaluation export, deployment setup, or hosted environment in this release.
- Product hypotheses remain unvalidated until a later evaluation phase.
