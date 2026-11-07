# Persona Telegram bot — Chat + Coding (DeepSeek V4.1 Flash + GLM-5.3 Flash via OpenRouter)

One bot, **two versions**:

- **💬 Chat** — an AI assistant with an owner-defined persona that it follows exactly.
- **💻 Coding** — a Claude Code–style coding agent: **DeepSeek V4.1 Flash** leads, plans and reviews, while
  **GLM-5.3 Flash agents** write and test code **in parallel** inside a real project folder, with full GitHub
  control and live CI monitoring.

Everything is shared between the two versions (persona, snippets, GitHub, keys, usage), but each has its
own **settings**, **keyboard**, **"/" command menu**, **settings panel** and **welcome screen**.
Switch with **💻 Coding mode** / **💬 Chat mode** on the keyboard, or `/code` and `/chat`.

All model-facing text (instructions, tool descriptions, tool results, logs) is English, to keep tokens light.

## 💬 Chat version

- 💭 **Live thinking** that collapses when done: "Thought for 12 seconds".
- ✍️ **The reply is written live**; text written before a web search is never lost.
- 📄 **Long replies are never split**: short replies come as one tidy message, long ones open as a Telegra.ph
  page inside Telegram (Instant View), complete.
- 🎨 **Clean formatting**: real bold, headings and lists, code blocks with a copy button. Right-to-left text
  (e.g. Arabic) still lays out correctly — this is display only and never reaches the model.
- ⏹ **Stop** a reply any time, 🔄 **regenerate** the latest one, ✏️ **edit** your message to get a new answer.
- 🔎 **Strong web search**: opens the best results and extracts their real text, filters by recency, reads any
  link in depth, and lists 🔗 **sources** under the reply.
- 🖼 **Images** (even albums) and 📎 **files** (PDF, text, code). ↩️ Reply to any message to ask about it.
- 🆕 **New chat**: forgets the conversation; the old messages stay in the chat.

## 💻 Coding version (like Claude Code)

### How it works

1. **The lead (DeepSeek V4.1 Flash)** understands the request, explores the project, writes a live plan
   (📋 todo list), and decides what to do itself and what to delegate.
2. It locks the shared **contracts** (structure, interfaces, signatures) first, then **delegates** with
   `dispatch_agents`: independent tasks run **in parallel**, dependent ones wait (`depends_on`) and receive the
   reports of what they depend on.
3. **The agents (GLM-5.3 Flash)** each get a precise brief (with the files they need pre-loaded), implement fully, **run the tests
   themselves**, and report back.
4. The lead **reviews, integrates and verifies** (runs the build/tests), fixes what's broken, and replies with a
   short summary.

### Identities for the lead and the agents

- `/setleadpersona` gives the lead its own identity (personality, voice, way of working). It replaces the Chat
  persona in Coding mode while **🎭 Persona voice** is on; without it the lead speaks in the Chat persona's voice.
- `/setagentpersona` gives every agent an identity, in every task (keep it short — it's sent every agent round).
- `/leadpersona` · `/agentpersona` show them; `/setleadpersona off` · `/setagentpersona off` remove them.
- Same input flow as `/setpersona` (text, several messages, or a `.txt` file), stored encrypted with `PERSONA_KEY`.
- Fixed rules wrap both (`rules.lead_identity` / `rules.agent_identity` in `config.json`): the identity shapes
  personality and working style, but never the quality bar, verification, safety, file scope or report format.

### Smart and safe parallel work

- Each agent owns a **file scope**; the system blocks writing into another running agent's scope.
- An agent can't overwrite a file it hasn't read, or one that changed since it read it.
- **Agent types**: `coder` (default), `explorer` (read-only research), `reviewer` (read-only bug hunt),
  `tester` — plus **custom agents** from `.agents/NAME.md` or `.claude/agents/NAME.md` (optional front matter:
  `description`, `readonly: true`, `model: …`). `/agents` lists them.

### The live screen

- 💭 the lead's thinking and every step (reads, edits, commands, delegation),
- 🛠 a **work panel**: the plan with live status, and every agent with what it's doing right now
  ("✍️ writing api/routes.py (~3k chars)"), collapsing at the end into a summary (time, agents, files ±lines,
  commands, cost),
- the final reply, with buttons: **↩️ Undo · 📄 Diff · 📦 Project (zip) · 🤖 Agent reports · 🔄 Regenerate**.

### Claude Code features

| Feature | How |
|---|---|
| Plan mode (plan → approve → execute) | `/plan`, then **✅ Run the plan** |
| Permission prompts | Coding settings → **🔐 Ask first**: commands and GitHub writes wait for **Allow / Allow all / Deny** (read-only commands like `ls`, `git diff` never ask) |
| Questions with buttons | the lead asks when an ambiguity really matters |
| Checkpoints / rewind | **↩️ Undo** or `/undo` (file-tool edits; like Claude Code, command side effects aren't tracked). 🔄 in Coding also restores files before redoing |
| Steering mid-run | just type while it works — the lead reads it at the next step |
| `!` shell | send `$ npm test` (or `/sh npm test`) |
| `#` memory | send `# use pytest for tests` → saved to `AGENTS.md` |
| Project memory (CLAUDE.md) | `AGENTS.md` (or `CLAUDE.md`) is read by the lead and every agent; `/init` writes it, `/memory` shows/edits it |
| @-mentions | `explain @src/app.py` attaches the file (or a folder listing) |
| Extended thinking | "ultrathink" / "think hard" in a message = maximum reasoning |
| Multi-edit | `edit_file` with `edits` (all apply or none) |
| Hooks | `.agents/hooks.json` → `{"after_edit": "ruff format {file} && ruff check {file}"}`; output goes back to the agent |
| Background tasks | servers run with `background`; `/jobs` lists them with ⏹ buttons |
| `/compact` `/context` `/cost` `/status` `/todos` `/export` `/doctor` | conversation compaction, token breakdown, usage, status, latest plan, session export, health check |
| `/review` `/security_review` `/pr_comments` | review changes or `owner/repo#12`; security audit; address PR comments |
| Uploads | send files or a **zip** — saved into the project (a zip is extracted) |

### Projects

Every project is its own folder: `/project NAME` switches/creates, `/project` lists them,
`/project delete NAME` deletes one, `/files` shows the tree, `/get PATH` sends a file, `/zip` sends everything,
`/clone owner/repo` clones a repo as a new project. Projects persist between runs (the Actions cache skips heavy
folders like `node_modules`).

### GitHub — full control and live CI

With `GH_PAT`, the lead controls the owner's GitHub: files, branches, PRs (details, diff, comments, merge),
issues, releases, code search, commits/compare — and **Actions**: runs, failed steps, **error logs**, re-run,
cancel, manual dispatch, and **watch** (follows CI live until it finishes, then returns the cause of any failure).

**The real-time fix loop:** read the failing log → clone the branch → reproduce locally → fix the root cause →
push → watch CI → repeat until green.

**Background watching:** `/watch owner/repo [branch]` sends you an alert on every failed run (with the error
excerpt) and buttons **🛠 Fix it with agents · 🔁 Re-run · 🔕 Stop watching**, plus a note when CI is green
again. `/watch owner/repo auto` lets the lead fix and push on its own (at most two automatic attempts in a row).

Token scopes: classic `repo` + `workflow`, or fine-grained Contents / Pull requests / Issues / Actions read & write.

### Speed (fewer, fuller rounds — same quality)

Most of a long run's time is round trips to the model, so the bot cuts rounds instead of cutting thinking:

- **Parallel tool calls:** independent read-only calls in one reply (reads, searches, listings, web, GitHub reads)
  run at the same time; `read_file` also takes `paths` to read several files in one call.
- **Pre-loaded agents:** `dispatch_agents` tasks take `read_first`; those files plus the agent's own existing files
  are loaded into its task, so it starts writing in its first round instead of spending rounds reading.
- **Review from the diff:** every agent's result carries its diff, so the lead reviews without re-reading files.
- **Instant syntax check** after every edit (Python, JSON, TOML, JS, shell), so a typo is fixed in the same round.
- Up to **6 agents** in parallel by default; the live panel shows elapsed time, tokens and cost.

### Token economy (without lowering quality)

- Old code versions are never resent: the lead gets the current file tree, `AGENTS.md`, and a short **project
  journal** (two lines per past request); the conversation memory keeps only the latest exchanges.
- A file that hasn't changed isn't sent twice in a session.
- Old tool results are trimmed in large, rare steps (keeps the prompt prefix stable for provider caching).
- **Superseded results are compacted:** an older read of a file that was read again or rewritten, an older run
  of the same command, and older copies of rewritten files — no information is lost, only duplicates.
- Heavy code volume goes to the GLM agents; the lead spends its reasoning on design, contracts and review.
- `/context` shows where the tokens go; `/compact` shrinks the conversation; **🎭 Persona voice** can be turned
  off in Coding settings.

### Safety

- Coding mode, shell commands and GitHub tools are **owner-only**; non-owners always get the normal Chat version.
- File tools can't leave the project folder; commands run with **secrets stripped from the environment**
  (tokens and keys never reach them); obviously destructive commands are refused; git inside a project can't
  touch the bot's own repository; the GitHub token is passed per command and never stored in a project.
- Commands still run on the runner machine — use **🔐 Ask first** if you want to approve each one.

## Persona and lock rules (exact, from Telegram)

| Method | |
|---|---|
| `/setpersona TEXT` / `/setrules TEXT` | saved exactly as written |
| `/setpersona` or `/setrules` alone | input mode: send one or several messages — after each, a fresh message with a preview and ✅ Save appears at the bottom |
| a `.txt` file with the caption `/setpersona` or `/setrules` | best for long texts |
| `/persona` / `/rules` | shows the current text + an exact file copy |

Edited lock rules are stored in `data/rules.json` and override `config.json`.

**Confidentiality:** non-owners get extra instructions not to reveal the persona or rules. This makes leaks
harder but can't guarantee it against a determined user.

**Encryption:** set the `PERSONA_KEY` secret
(`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) and re-set the
persona and rules — they're then stored encrypted. Without the right key the bot falls back to the defaults,
so don't lose it.

> ⚠️ The previous persona/rules are still in this public repository's **git history**. Hiding them now doesn't
> erase history: rewrite history, make the repository private, or start a fresh repository.

## Snippets

`/snip NAME TEXT` saves text (or reply to a message/file with `/snip NAME`); write `!NAME` in any message to
expand it. `/snippets` lists them, `/delsnip NAME` deletes one. Owner only.

## Commands

| Command | |
|---|---|
| `/new` | new chat (Chat) or new session (Coding — the project files and journal stay) |
| `/code` · `/chat` | switch versions |
| `/settings` | the current version's settings panel |
| `/set SETTING VALUE` | e.g. `/set temperature 0.7`, `/set max_agents 6`, `/set permissions ask` (`off` = default) |
| `/settitle NAME` · `/title` | the X-Title sent to OpenRouter (Latin characters only), stored encrypted |
| `/effort` `/thinking` `/search` | quick toggles |
| `/resetall` | wipe every conversation memory |
| Coding | `/project` `/files` `/get` `/zip` `/sh` `/undo` `/diff` `/plan` `/memory` `/init` `/review` `/security_review` `/pr_comments` `/compact` `/context` `/status` `/jobs` `/todos` `/export` `/agents` `/doctor` `/cost` `/clone` `/push` `/watch` |

One-message settings (owner): `effort=max web_search=off your question`.

## Settings

**Chat:** model, reasoning effort, temperature, top_p, max_tokens, show thinking, web search, long
replies as a page, txt file after each reply.

**Coding (independent):** lead model, lead reasoning (default **high**), agent model
(`z-ai/glm-5.3-flash`), max parallel agents (default 6), plan first, permissions
(auto / ask), persona voice, show thinking, web search, long replies as a page, txt file.

## OpenRouter

- Every model runs on **one fixed provider** with `allow_fallbacks: false` — set in `config.json`
  (`model_providers`) and not changeable from Telegram:
  - `deepseek/deepseek-v4.1-flash-1017` → `deepinfra/fp8`
  - `z-ai/glm-5.3-flash` → `relace/fp4`
- Old model names (Kimi K3.1 Pro, GLM-5.3) saved in settings or variables move to the new models automatically.
- Requests send `reasoning: {enabled: true}` (or `effort` when set); Coding also asks for `usage` to track cost.
- `reasoning_details` is passed back unchanged between tool rounds and across the last 10 chat replies.
- Effort levels: default · low · medium · high · max (`xhigh` on OpenRouter).
- If a provider doesn't support tools: Chat continues without search; Coding needs tools.

## Two backends: 💳 paid (OpenRouter) / 🆓 free (NVIDIA)

The same models run on either backend, switched by hand: `/backend` (buttons), `/free`, `/paid`, or the
**🔀 Backend** row in both settings panels. Everything follows the switch — Chat, the Coding lead and the agents.

- **💳 OpenRouter:** each model on its fixed provider (above).
- **🆓 NVIDIA NIM** (`integrate.api.nvidia.com`): `deepseek-ai/deepseek-v4.1-flash` and `z-ai/glm-5.3-flash`
  (mapping in `config.json` → `nvidia.models`; optional per-model request fields in `nvidia.model_extra`).
- **Several keys, load spread over them** (`backends.KeyPool`): each request goes to the key with the fewest
  requests in the last minute; no key goes over `rpm_per_key` (default 40) — when all are full the request waits for
  the first free slot (up to `max_wait_seconds`); a 429 rests that key and the request moves to another one at once;
  a 401/403 takes the key out; a bad request (400) isn't retried.
- **`/ping`** checks every key for real (a 1-token request each, in parallel) plus the OpenRouter key, and shows
  each NVIDIA key's load. `/doctor` and `/status` show the backend too.
- Secrets: `NVIDIA_KEY_1`, `NVIDIA_KEY_2`, `NVIDIA_KEY_3` (bot 2 uses `NVIDIA_KEY_2_1..3` if set, otherwise the same
  three). Never paste keys into code or chat.

## Web via Tor / a proxy (optional)

All web requests (search + opening pages) can go through Tor or any SOCKS proxy: set the variable
`USE_TOR=true` on GitHub (installs and starts Tor on the runner), or set `TOR_PROXY`/`WEB_PROXY`
(e.g. `socks5h://127.0.0.1:9050`) or `web_proxy` in `config.json`.

## Running 24/7 on GitHub Actions

The bot runs on GitHub Actions; every ~6 hours it restarts itself (about a minute), and its data (persona,
memory, projects) is kept between runs.

1. **Settings → Secrets and variables → Actions → Secrets:** `TELEGRAM_BOT_TOKEN` · `OPENROUTER_API_KEY` ·
   `OWNER_ID` · optional `GH_PAT` · optional `PERSONA_KEY`.
2. Optional **Variables:** `OPENROUTER_MODEL`, `AGENT_MODEL`, `USE_TOR`.
3. **Actions → Telegram bot 24h → Run workflow.**

> ⚠️ Running a permanent service on GitHub Actions goes against GitHub's terms; a real server is recommended.
> Don't run the bot locally and on GitHub at the same time — Telegram accepts one copy only.

## A second bot on GLM-5.3 Flash (bot 2)

`.github/workflows/bot2.yml` runs a second, fully independent bot with **the same code and every feature**
(Chat + Coding), on `z-ai/glm-5.3-flash` via `relace/fp4` — in Coding mode it is both the lead and the agents.
It has its own token, persona, rules, memory and projects (cache `bot2-data-`).

- Required: the secret `TELEGRAM_BOT_TOKEN_2` (a different bot from @BotFather).
- Shared with bot 1 by default: `OPENROUTER_API_KEY`, `OWNER_ID`, `GH_PAT` (override with `_2` secrets).
- Optional: `PERSONA_KEY_2`; variables `OPENROUTER_MODEL_2`, `AGENT_MODEL_2`, `USE_TOR_2`.
- Start it: **Actions → Telegram bot 2 (GLM Flash) 24h → Run workflow** (it also starts on every push).

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the values
python bot.py
```

## Files

- `bot.py` — the bot: commands, live replies, buttons, persona, both versions.
- `agent.py` — Coding mode: tools, the agent loop, parallel scheduling and tool calls, the work panel.
- `workspace.py` — project folders: files, commands, background jobs, checkpoints, file claims, git.
- `llm.py` — streaming from OpenRouter (shared by chat, the lead and the agents).
- `prompts/orchestrator.md` · `prompts/worker.md` — the lead's and the agents' instructions (editable).
- `github_tools.py` — GitHub tools (owner only), including Actions and live CI watching.
- `tools.py` — web search and page reading.
- `render.py` · `telegraph.py` — Telegram formatting and Telegra.ph pages.
- `config.py` / `config.json` — commands, buttons, settings and rules (edit `config.json`, no code needed).
- `vault.py` — encryption of the persona, rules and X-Title.
- `tests/test_e2e.py` — end-to-end tests with fake Telegram/OpenRouter/Telegra.ph/GitHub servers:
  `python tests/test_e2e.py`.
