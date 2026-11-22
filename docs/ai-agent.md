# AI agent (`sudo agent`)

A native coding and operations agent with its own page: chats that stream as they are written, tool cards, colour
diffs, approval prompts, and saved sessions you can continue, branch and export. It reads, searches, edits and runs
things **inside your current target (host or VM, see `vm mode`)**, the way Claude Code, OpenCode or Codex do, but
built for a 512 MB / 0.1 CPU box. Admins only (it can run commands, like `sudo`).

```bash
sudo agent                       # open the agent page (new chat, this tab's directory)
sudo agent "add tests for utils.py"   # open it and start the task
sudo agent -c                    # continue the latest chat        (-r <id> resumes one, `agent ls` lists them)
sudo agent -p "what changed?"    # run a task right here and print the answer (edits freely, asks nothing)
sudo agent --cli                 # a plain line-by-line session in this console, if you do not want the page
```

The console menu and **Dashboard → AI agent** open the page too.

## Providers: set them up in the dashboard

**Dashboard → AI agent** lists every provider with its key status. Paste a key, optionally change the address or
model, **Test** it, **Browse** its models (a searchable list that also works with a key you have typed but not saved yet), and make one the default. Keys are stored on the server (`data/agent/providers.json`,
mode 0600), shown as "Saved" and never sent back to the browser. Built in:

OpenCode Zen · Anthropic (Claude, with prompt caching) · OpenAI · **OpenRouter** · **Hugging Face** (Inference Providers
router) · Groq · Google Gemini · DeepSeek · Mistral · xAI · Together · Cerebras · Fireworks · NVIDIA NIM · Ollama and
LM Studio (local, no key). **Add your own** covers any OpenAI-compatible server (vLLM, LiteLLM, a gateway, ...). Advanced
fields per provider: context window, max output tokens, extra headers and extra request body (for example OpenRouter
routing). A key set the old way (`sudo env set OPENROUTER_API_KEY=...`, `HF_TOKEN`, `OPENAI_API_KEY`, ...) or in the
server environment works as well; `<PROVIDER>_MODEL` and `<PROVIDER>_BASE_URL` still override the defaults.

## In a chat

| | |
|---|---|
| **Tools** | `read`, `edit` (exact replace, multi-edit, CRLF-safe), `write`, `grep`, `glob`, `ls`, `bash` (persistent `cd`, live output, timeout, background jobs), `web_fetch`, `web_search`, `todo_write`, `ask_user`, `skill`, `rconsole_docs` |
| **Modes** | **Plan** (read-only, then asks you to approve a plan), **Ask** (asks before edits and commands), **Edit** (edits freely), **Auto** (never asks). Read-only commands such as `git status` never ask; "Always allow" remembers a command per chat |
| **Sessions** | every chat is saved (append-only), listed by day with search; rename, branch, delete, export as Markdown; a turn keeps running if your phone drops the connection and the page picks it up again |
| **Slash commands** | `/help /new /sessions /model /provider /mode /plan /compact /context /undo /cwd /rename /fork /init /todos /export /memory /cost`, plus your own from `.rconsole/commands/*.md` (`$ARGUMENTS` is replaced) |
| **Context** | `@path` inlines a file; `AGENTS.md` / `CLAUDE.md` in the project (and the dashboard's standing instructions) are read automatically; `/undo` restores the files the last turn changed |

## It knows where it runs

The agent's system prompt is built for every turn from the live state, so it behaves like a person who knows the place:

- **Rconsole itself.** It knows it runs inside Rconsole (made by Md Abu Salehin), that `vm`, `service`, `expose`, `storage`,
  `backup`, `env`, `keepalive`, `rconsole update`... are **console commands** that do not exist in its shell, and gives you the
  exact line to type instead of trying to run them. For details of any command it looks the reference up with the
  `rconsole_docs` tool rather than guessing. A test fails if a console command is missing from the agent's manual.
- **Where it is.** Host or VM, which Linux (so `apt-get`, `apk` or `pacman`), the working directory and what is in it, the
  landing folder and the home directory. Inside a VM it installs software with the VM's own tools (`apt-get install`, `pip`,
  `npm`) and never runs `vm install`, which would create a whole new Linux system.
- **Where to put things.** If the working directory already is a project (it has `.git`, `package.json`, `pyproject.toml`,
  `manage.py`, `index.html`, ...) it works right there. If it is the general landing folder and you ask for something new (a
  website, a bot), it creates a short, named project folder and builds inside it, then tells you the path, the command to run
  it and the `expose` / `service add` line to publish it. A path you name is always used as given.

## Skills

Skills are saved instructions for a kind of task, in the same format OpenCode and Claude Code use: a folder with a
`SKILL.md` (front matter with `name` and `description`, then the steps) and optional scripts or reference files.

| | |
|---|---|
| **Where they live** | `.rconsole/skills/<name>/` in the project (and its parent folders) or `~/.rconsole/skills/<name>/` for every project. `.claude/skills`, `.agents/skills` and `.opencode/skills` are read as well |
| **How they are used** | Only each skill's name and one-line description are in the prompt. When a task matches, the agent loads the full text with the `skill` tool and follows it (reading the skill's own files when told to). You can also run one yourself: `/<name> your request` |
| **Manage** | `/skills` lists them · `/skills add <owner/repo or github link> [names]` installs from GitHub (`/skills add anthropics/skills frontend-design`) · `/skills new <name>` creates a template · `/skills remove <name>`. Add `--project` to install or create inside the working directory instead of your home |

A skill is text the agent will follow, so install only skills you trust. The model cannot install one on its own, and
`/skills add` only downloads from github.com.

## Built for small machines

- File tools work on the filesystem directly (no shell start-up per read or search); only `bash` spawns a process.
- One streaming HTTP request per model step; read-only tool calls of one step run in parallel (at most 4 threads).
- The transcript is appended one short line per message, only the last few chats stay in memory, and the chat list
  never opens a transcript.
- Context is managed in two cheap stages: old tool output is replaced by a stub in rare batches (so provider prompt
  caches stay valid), and only above the dashboard's threshold is the earlier chat summarised with one extra call.
  Every tool result the model sees is size-capped (head and tail kept).
- At most *N* chats run at once (dashboard, default 2); a turn stops after a number of steps (default 40) or when the
  model repeats itself.

## Safety

- Admins only. File tools refuse the server's own secrets (`secret.key`, `users.json`, `settings.json`, every user's
  stored environment and the provider keys), and known keys are scrubbed from tool output and error text.
- On the *host* the `bash` tool refuses obviously destructive commands (`rm -rf /`, `mkfs`, `dd` onto disks,
  `shutdown`, ...); inside your **VM** the guard is off because those are your own files (`vm reset` rebuilds it).
- A page or file the agent reads is treated as data, never as instructions; `web_fetch` refuses cloud-metadata addresses.
- **Auto** mode runs commands without asking, so use it for work you would run yourself.

**Built-in skills.** `find-skills` ships with Rconsole (folder `bundled_skills/`) and is always listed by `/skills` as `[built-in]`. It helps you find skills and prints the `/skills add` command to run; the agent never installs one itself. A skill of your own with the same name takes priority; built-in skills cannot be removed.

**Context window.** The agent finds a model's window by itself: the number you set for the provider wins, then what the provider reports in its own model list (OpenRouter, Groq, Together, Hugging Face, vLLM, Anthropic and others do; looked up once per ten minutes), then a table of well-known models, and only then a 32k default. Set "context window" on the provider if yours reports nothing and the default is wrong.
