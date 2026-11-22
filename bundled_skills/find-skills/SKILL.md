---
name: find-skills
description: Helps the person discover agent skills when they ask "how do I do X", "find a skill for X", "is there a skill that can...", or want to extend what the agent can do. Use it when a task might already have an installable skill.
---

# Find Skills

Adapted for Rconsole from the open `find-skills` skill (vercel-labs/skills, MIT). Skills are folders with a `SKILL.md` that teach the agent a task. This skill helps you find good ones. **You never install a skill yourself**: a skill is text you will follow, so the person installs it, after seeing what it is.

## When to use it

- "how do I do X" where X is a common task with a likely skill (testing, design, deployment, documents, ...)
- "find a skill for X", "is there a skill that...", "can you do X" for a specialised job
- the person wants to extend the agent

## Steps

1. **Understand the need**: the domain and the specific task. First check the skills already installed: they are listed in your instructions (the skills catalog), and the person can see them with the `/skills` chat command. `/skills` is a chat command, **not a folder or a tool**; never list it with a file tool. If one fits, load it with the `skill` tool.
2. **Search**:
   - Look at the leaderboard https://skills.sh/ with the web fetch tool, and search GitHub for repositories that hold `SKILL.md` files.
   - If Node.js is available you may also run `npx skills find <query>` (search only; never `npx skills add`).
   - Well-known sources: `anthropics/skills` (frontend-design, documents), `vercel-labs/agent-skills` (React, Next.js, web design).
3. **Check quality before recommending**: prefer known authors and repositories with many stars or installs; read the `SKILL.md` first (raw.githubusercontent.com) and mention anything that runs commands or fetches things.
4. **Present 1 to 3 options**: name, one line on what it does, the source link, and the exact command for the person to type:

   ```
   /skills add <owner>/<repo> <skill-name>
   ```

   Add `--project` to keep it inside the working directory only. Example: `/skills add anthropics/skills frontend-design`.
5. **If nothing fits**: say so, do the task directly, and offer to write a skill of their own with `/skills new <name>`.

Do not install anything on your own, even if the person says "just get it". Tell them the command and let them run it.
