"""AI providers for the Rconsole agent: the catalogue, where keys live, and streaming chat transports.

Two wire formats cover everything:

* ``openai``    - ``POST {base}/chat/completions`` (OpenAI, OpenRouter, Hugging Face, Groq, DeepSeek, Gemini's
                  compatibility endpoint, Mistral, xAI, Together, Cerebras, NVIDIA, Fireworks, Ollama, LM Studio,
                  OpenCode Zen and any custom OpenAI-compatible server);
* ``anthropic`` - ``POST {base}/v1/messages`` (Claude), with prompt caching.

Both stream (server-sent events), so text appears while it is generated and one turn is one HTTP request.
Nothing here needs a dependency beyond ``requests``.

Keys come from, in order: the dashboard (``<data dir>/agent/providers.json``, mode 0600, write-only in the
dashboard), the user's ``sudo env`` store, the server's environment. The key never reaches the browser and is
scrubbed from error text.

Canonical transcript format (what :mod:`agent_store` saves)::

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "args": {...}}]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}
"""
import hashlib
import json
import os
import re
import threading
import time

import requests

import config

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 120               # between two chunks of a stream
RETRIES = 3
DEFAULT_MAX_TOKENS = 8192

# --------------------------------------------------------------------------------------- catalogue
# id, name, kind, base url, environment variables that may hold the key, default model, needs key, note
_P = [
    ("opencode", "OpenCode Zen", "openai", "https://opencode.ai/zen/v1", ("OPENCODE_API_KEY", "ZEN_API_KEY"),
     "mimo-v2.5-free", True, "Curated coding models, several free. Key: opencode.ai/auth"),
    ("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com", ("ANTHROPIC_API_KEY",),
     "claude-sonnet-4-5", True, "Claude models with prompt caching. Key: console.anthropic.com"),
    ("openai", "OpenAI", "openai", "https://api.openai.com/v1", ("OPENAI_API_KEY",),
     "gpt-4o-mini", True, "Key: platform.openai.com/api-keys"),
    ("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",),
     "openrouter/auto", True, "One key for hundreds of models, many free (suffix :free). Key: openrouter.ai/keys"),
    ("huggingface", "Hugging Face", "openai", "https://router.huggingface.co/v1",
     ("HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN"),
     "Qwen/Qwen3-Coder-30B-A3B-Instruct", True,
     "Inference Providers router. Add :fastest or :cheapest to a model id. Token: huggingface.co/settings/tokens"),
    ("groq", "Groq", "openai", "https://api.groq.com/openai/v1", ("GROQ_API_KEY",),
     "llama-3.3-70b-versatile", True, "Very fast inference, generous free tier. Key: console.groq.com/keys"),
    ("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
     ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "gemini-2.5-flash", True,
     "Gemini through its OpenAI-compatible endpoint. Key: aistudio.google.com/apikey"),
    ("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", ("DEEPSEEK_API_KEY",),
     "deepseek-chat", True, "Key: platform.deepseek.com"),
    ("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", ("MISTRAL_API_KEY",),
     "mistral-medium-latest", True, "Key: console.mistral.ai"),
    ("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", ("XAI_API_KEY",),
     "grok-code-fast-1", True, "Key: console.x.ai"),
    ("together", "Together AI", "openai", "https://api.together.xyz/v1", ("TOGETHER_API_KEY",),
     "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8", True, "Key: api.together.ai"),
    ("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1", ("CEREBRAS_API_KEY",),
     "qwen-3-coder-480b", True, "Very fast inference. Key: cloud.cerebras.ai"),
    ("fireworks", "Fireworks AI", "openai", "https://api.fireworks.ai/inference/v1", ("FIREWORKS_API_KEY",),
     "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct", True, "Key: fireworks.ai"),
    ("nvidia", "NVIDIA NIM", "openai", "https://integrate.api.nvidia.com/v1", ("NVIDIA_API_KEY",),
     "meta/llama-3.3-70b-instruct", True, "Key: build.nvidia.com"),
    ("ollama", "Ollama (local)", "openai", "http://localhost:11434/v1", ("OLLAMA_API_KEY",),
     "qwen2.5-coder", False, "A model server on the same machine or network. No key needed."),
    ("lmstudio", "LM Studio (local)", "openai", "http://localhost:1234/v1", ("LMSTUDIO_API_KEY",),
     "local-model", False, "No key needed."),
]
PRESETS = {p[0]: {"id": p[0], "name": p[1], "kind": p[2], "base": p[3], "env": list(p[4]), "model": p[5],
                  "needs_key": p[6], "note": p[7], "custom": False} for p in _P}
ORDER = [p[0] for p in _P]
# Back-compat order used when no default is chosen: the first provider that has a key wins.
_CTX = [   # (regex on the model id, context window in tokens): a hint for compaction; the dashboard can override
    (r"claude", 200000), (r"gemini", 1000000), (r"gpt-5", 400000), (r"gpt-4\.1", 1000000), (r"gpt-4o|o\d", 128000),
    (r"qwen3-coder", 256000), (r"deepseek", 128000), (r"llama-?3\.[123]|llama-?4", 128000), (r"kimi|glm|minimax", 128000),
    (r"mistral|codestral", 128000), (r"grok", 256000),
]
DEFAULT_CTX = 32768

_lock = threading.RLock()
_models_cache = {}               # (base, key hash) -> (time, [ids])


class ProviderError(Exception):
    def __init__(self, message, status=None, retryable=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------------- stored config
def _path():
    return config.DATA_DIR / "agent" / "providers.json"


def load():
    """The saved dashboard configuration: ``{"default": id, "providers": {...}, "options": {...}}``."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("providers", {})
            data.setdefault("options", {})
            return data
    except (OSError, ValueError):
        pass
    return {"default": "", "providers": {}, "options": {}}


def save(data):
    p = _path()
    config.ensure_private_dir(p.parent)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def _clean_id(s):
    return re.sub(r"[^a-z0-9_-]", "", (s or "").lower())[:32]


def _env_value(names, username):
    """The first non-empty value among ``names`` in the user's ``sudo env`` store, then the process env."""
    env = {}
    try:
        import state
        env = state.get_env(username) or {} if username else {}
    except Exception:
        pass
    for n in names:
        v = (env.get(n) or os.environ.get(n) or "").strip()
        if v:
            return v, ("sudo env" if env.get(n) else "environment"), n
    return "", "", ""


def provider_table(username=""):
    """Every provider (presets + custom) merged with what is saved: the dashboard's and the agent's view.

    Never contains a key; ``has_key`` / ``key_source`` say whether and where one was found.
    """
    data = load()
    saved = data.get("providers", {})
    rows = []
    ids = list(ORDER) + [i for i in saved if i not in PRESETS]
    for pid in ids:
        base = PRESETS.get(pid)
        s = saved.get(pid, {}) if isinstance(saved.get(pid), dict) else {}
        if base is None:
            base = {"id": pid, "name": s.get("name") or pid, "kind": s.get("kind") or "openai", "base": "",
                    "env": [], "model": "", "needs_key": bool(s.get("needs_key", True)), "note": "", "custom": True}
        row = dict(base)
        # Older versions took the model and address from the environment (OPENCODE_MODEL, OPENAI_BASE_URL, ...):
        # still honoured when nothing was saved in the dashboard.
        env_model = _env_value([pid.upper() + "_MODEL"], username)[0]
        env_base = _env_value([pid.upper() + "_BASE_URL"], username)[0]
        row["base"] = (s.get("base") or env_base or base["base"]).strip().rstrip("/")
        row["model"] = (s.get("model") or env_model or base["model"]).strip()
        row["enabled"] = s.get("enabled", True) is not False
        row["headers"] = s.get("headers") if isinstance(s.get("headers"), dict) else {}
        row["extra_body"] = s.get("extra_body") if isinstance(s.get("extra_body"), dict) else {}
        row["context"] = int(s.get("context") or 0)
        row["max_tokens"] = int(s.get("max_tokens") or 0)
        key = (s.get("api_key") or "").strip()
        src, envname = "dashboard", ""
        if not key:
            key, src, envname = _env_value(row["env"], username)
        row["has_key"] = bool(key)
        row["key_source"] = src if key else ""
        row["key_env"] = envname
        row["usable"] = row["enabled"] and bool(row["base"]) and (bool(key) or (not row["needs_key"] and pid in saved))
        row["_key"] = key
        rows.append(row)
    return rows


def public_table(username=""):
    rows = provider_table(username)
    for r in rows:
        r.pop("_key", None)
    return rows


def resolve(username="", provider=None, model=None):
    """The provider to talk to as a ready config dict (with its key), or ``None`` when nothing is usable."""
    rows = provider_table(username)
    by_id = {r["id"]: r for r in rows}
    chosen = by_id.get(provider or "")
    if not (chosen and chosen["usable"]):
        d = by_id.get(load().get("default") or "")
        chosen = d if d and d["usable"] else next((r for r in rows if r["usable"]), None)
    if not chosen:
        return None
    cfg = dict(chosen)
    cfg["api_key"] = cfg.pop("_key", "")
    if model:
        cfg["model"] = model
    return cfg


def config_for(pid, username=""):
    """The ready config of one specific provider (no fallback to another), or ``None`` if it has no key/URL yet."""
    for r in provider_table(username):
        if r["id"] == pid and r["base"] and (r["_key"] or not r["needs_key"]):
            cfg = dict(r)
            cfg["api_key"] = cfg.pop("_key", "")
            return cfg
    return None


def browse_config(username, pid, base="", api_key=""):
    """Config used to list a provider's models: that exact provider (usable or not), optionally with an address
    or key typed in the dashboard but not saved yet. Returns ``None`` for an unknown provider; raises ``ValueError``
    for a bad address."""
    row = next((r for r in provider_table(username) if r["id"] == pid), None)
    if row is None:
        return None
    cfg = dict(row)
    key = cfg.pop("_key", "") or ""
    base = (base or "").strip().rstrip("/")
    if base:
        if not re.match(r"^https?://[^\s]+$", base):
            raise ValueError("the server address must start with http:// or https://")
        cfg["base"] = base
    if not cfg.get("base"):
        raise ValueError("this provider has no server address yet")
    if (api_key or "").strip():
        key = api_key.strip()
    cfg["api_key"] = key
    return cfg


def context_window(cfg):
    if cfg.get("context"):
        return int(cfg["context"])
    m = (cfg.get("model") or "").lower()
    for pat, n in _CTX:
        if re.search(pat, m):
            return n
    return DEFAULT_CTX


def _num(o, key, default, lo, hi):
    try:
        v = o.get(key)
        v = default if v in (None, "") else int(v)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def options():
    """Global agent options from the dashboard, with defaults (every number is clamped to a safe range)."""
    o = load().get("options", {})
    return {
        "mode": o.get("mode") if o.get("mode") in ("plan", "ask", "edit", "auto") else "ask",
        "max_steps": _num(o, "max_steps", 40, 5, 200),
        "max_runs": _num(o, "max_runs", 2, 1, 8),
        "compact_pct": _num(o, "compact_pct", 80, 40, 95),
        "bash_timeout": _num(o, "bash_timeout", 120, 10, 900),
        "instructions": str(o.get("instructions") or "")[:8000],
    }


def save_provider(pid, fields):
    """Create or update one provider from dashboard input. ``fields['api_key']`` of ``None`` keeps the old key."""
    pid = _clean_id(pid)
    if not pid:
        raise ValueError("a provider needs a short id (letters, digits, - or _)")
    with _lock:
        data = load()
        cur = dict(data["providers"].get(pid) or {})
        for k in ("name", "kind", "base", "model", "needs_key"):
            if k in fields and fields[k] is not None:
                cur[k] = fields[k]
        if "base" in cur:
            base = str(cur["base"]).strip().rstrip("/")
            if base and not re.match(r"^https?://[^\s]+$", base):
                raise ValueError("the base URL must start with http:// or https://")
            cur["base"] = base
        if pid not in PRESETS and not cur.get("base"):
            raise ValueError("a custom provider needs a base URL")
        if cur.get("kind") not in (None, "openai", "anthropic"):
            raise ValueError("kind must be openai or anthropic")
        if "enabled" in fields:
            cur["enabled"] = bool(fields["enabled"])
        if fields.get("api_key") is not None:
            key = str(fields["api_key"]).strip()
            if key:
                cur["api_key"] = key
            else:
                cur.pop("api_key", None)
        for k, cap in (("context", 4_000_000), ("max_tokens", 200_000)):
            if k in fields:
                try:
                    n = int(fields[k] or 0)
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number")
                cur[k] = max(0, min(n, cap))
        for k in ("headers", "extra_body"):
            if k in fields:
                v = fields[k]
                if isinstance(v, str):
                    v = json.loads(v) if v.strip() else {}
                if not isinstance(v, dict):
                    raise ValueError(f"{k} must be a JSON object")
                if k == "headers":
                    old = cur.get("headers") or {}
                    # "***" is how the dashboard shows a secret header it must not reveal: keep the saved value
                    v = {str(a)[:60]: (old.get(a, "") if str(b) == "***" else str(b)[:300]) for a, b in v.items()}
                cur[k] = v
        data["providers"][pid] = cur
        save(data)
    _models_cache.clear()
    return pid


def remove_provider(pid):
    with _lock:
        data = load()
        had = data["providers"].pop(pid, None) is not None
        if data.get("default") == pid:
            data["default"] = ""
        save(data)
    _models_cache.clear()
    return had


def set_default(pid):
    with _lock:
        data = load()
        data["default"] = pid
        save(data)


def set_options(opts):
    with _lock:
        data = load()
        cur = data.setdefault("options", {})
        for k, v in opts.items():
            if k in ("mode", "max_steps", "max_runs", "compact_pct", "bash_timeout", "instructions"):
                cur[k] = v
        save(data)


def known_keys(username=""):
    """Every key value currently configured (used to scrub tool output and error text)."""
    return [r["_key"] for r in provider_table(username) if r.get("_key") and len(r["_key"]) >= 8]


def scrub(text, keys):
    for k in keys or ():
        if k and k in text:
            text = text.replace(k, "***")
    return text


# ---------------------------------------------------------------------------------------- transports
def _headers(cfg):
    h = {"Content-Type": "application/json", "Accept": "text/event-stream", "Accept-Encoding": "identity",
         "User-Agent": "Rconsole-agent"}
    key = cfg.get("api_key") or ""
    if cfg["kind"] == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = "2023-06-01"
    elif key:
        h["Authorization"] = "Bearer " + key
    if cfg["id"] == "openrouter":
        h["HTTP-Referer"] = "https://github.com/Salehin-07/rconsole-ark"
        h["X-Title"] = "Rconsole"
    h.update(cfg.get("headers") or {})
    return h


def _explain(status, body, cfg):
    msg = ""
    try:
        j = json.loads(body)
        e = j.get("error", j) if isinstance(j, dict) else j
        msg = (e.get("message") if isinstance(e, dict) else str(e)) or ""
    except Exception:
        msg = (body or "")[:300]
    msg = " ".join(str(msg).split())[:400]
    hint = {
        401: "the API key was rejected - check it in Dashboard > AI agent",
        403: "the key is not allowed to do that (permissions, region or credit)",
        404: "model or URL not found - check the model name and base URL",
        402: "the account has no credit left",
        429: "rate limited - wait a moment or pick another model",
    }.get(status, "")
    out = f"{cfg['name']} answered HTTP {status}"
    if msg:
        out += f": {msg}"
    if hint:
        out += f" ({hint})"
    return out


def _chunks(resp, cancel):
    """Yield raw byte chunks as they arrive (not waiting for a full buffer)."""
    raw = resp.raw
    read = getattr(raw, "read1", None)
    if read is None:
        yield from resp.iter_content(chunk_size=1024)
        return
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        b = read(16384)
        if not b:
            return
        yield b


def _sse(resp, cancel):
    """Yield ``(event, data_str)`` for each server-sent event."""
    buf = b""
    for chunk in _chunks(resp, cancel):
        buf += chunk
        while True:
            m = re.search(rb"\r?\n\r?\n", buf)
            if not m:
                break
            block, buf = buf[:m.start()], buf[m.end():]
            event, data = "", []
            for line in block.splitlines():
                if line.startswith(b":"):
                    continue
                if line.startswith(b"event:"):
                    event = line[6:].strip().decode("utf-8", "replace")
                elif line.startswith(b"data:"):
                    data.append(line[5:].lstrip().decode("utf-8", "replace"))
            if data:
                yield event, "\n".join(data)
    if buf.strip():
        data = [ln[5:].lstrip().decode("utf-8", "replace") for ln in buf.splitlines() if ln.startswith(b"data:")]
        if data:
            yield "", "\n".join(data)


def _open(cfg, url, body, cancel, on_retry=None):
    """POST with retries on 429/5xx/connection trouble (only before any output)."""
    keys = [cfg.get("api_key") or ""]
    last = None
    for attempt in range(RETRIES + 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        delay = min(12.0, 1.5 * 2 ** attempt)
        try:
            r = requests.post(url, headers=_headers(cfg), json=body, stream=True,
                              timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as e:
            last = ProviderError(scrub(f"cannot reach {cfg['name']}: {e.__class__.__name__}: {e}", keys)[:300],
                                 retryable=True)
        else:
            if r.status_code < 400:
                return r
            try:
                text = r.text
            finally:
                r.close()
            retry = r.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 529)
            last = ProviderError(scrub(_explain(r.status_code, text, cfg), keys), status=r.status_code,
                                 retryable=retry)
            if not retry:
                raise last
            wait = r.headers.get("Retry-After", "")
            if wait.replace(".", "", 1).isdigit():
                delay = min(20.0, float(wait))
        if attempt == RETRIES:
            break
        if on_retry:
            on_retry(attempt + 1, delay, str(last))
        end = time.time() + delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(0.1)
    raise last


def _oa_messages(system, messages):
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        r = m["role"]
        if r == "assistant":
            d = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                d["tool_calls"] = [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}}
                                   for c in m["tool_calls"]]
                if not d["content"]:
                    d["content"] = None
            out.append(d)
        elif r == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("content") or ""})
        else:
            out.append({"role": "user", "content": m.get("content") or ""})
    return out


def _oa_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["parameters"]}} for t in tools]


def _stream_openai(cfg, system, messages, tools, cancel, cb, max_tokens):
    url = cfg["base"].rstrip("/") + "/chat/completions"
    body = {"model": cfg["model"], "messages": _oa_messages(system, messages), "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = _oa_tools(tools)
        body["tool_choice"] = "auto"
    if max_tokens:
        body["max_tokens"] = max_tokens
    body.update(cfg.get("extra_body") or {})
    try:
        resp = _open(cfg, url, body, cancel, cb.get("retry"))
    except ProviderError as e:
        # A few servers reject stream_options; retry once without it.
        if e.status in (400, 422) and "stream_options" in str(e).lower():
            body.pop("stream_options", None)
            resp = _open(cfg, url, body, cancel, cb.get("retry"))
        else:
            raise
    text, calls, usage, finish = [], {}, {}, ""
    try:
        for _ev, data in _sse(resp, cancel):
            if data.strip() == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("error"):
                e = obj["error"]
                raise ProviderError(scrub(f"{cfg['name']}: {e.get('message') if isinstance(e, dict) else e}",
                                          [cfg.get("api_key") or ""])[:400])
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    text.append(d["content"])
                    cb["text"](d["content"])
                th = d.get("reasoning_content") or d.get("reasoning")
                if th and isinstance(th, str):
                    cb["think"](th)
                for tc in d.get("tool_calls") or []:
                    i = tc.get("index", 0)
                    slot = calls.setdefault(i, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    f = tc.get("function") or {}
                    if f.get("name"):
                        slot["name"] += f["name"]
                    if f.get("arguments"):
                        slot["args"] += f["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
    finally:
        resp.close()
    tool_calls = []
    for i in sorted(calls):
        c = calls[i]
        if not c["name"]:
            continue
        try:
            args = json.loads(c["args"]) if c["args"].strip() else {}
            if not isinstance(args, dict):
                raise ValueError("arguments are not an object")
            bad = ""
        except ValueError as e:
            args, bad = {}, f"the tool arguments were not valid JSON ({e}): {c['args'][:200]}"
        tool_calls.append({"id": c["id"] or f"call_{int(time.time() * 1000)}_{i}", "name": c["name"],
                           "args": args, "bad": bad})
    u = {"input": int(usage.get("prompt_tokens") or 0), "output": int(usage.get("completion_tokens") or 0),
         "cached": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)}
    if usage.get("cost") is not None:
        try:
            u["cost"] = float(usage["cost"])
        except (TypeError, ValueError):
            pass
    return {"content": "".join(text), "tool_calls": tool_calls, "usage": u, "finish": finish}


def _an_messages(messages):
    out = []

    def push(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)      # consecutive tool results become one user turn
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        r = m["role"]
        if r == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("args") or {}})
            if blocks:
                push("assistant", blocks)
        elif r == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m.get("content") or ""}])
        else:
            push("user", [{"type": "text", "text": m.get("content") or ""}])
    return out


def _stream_anthropic(cfg, system, messages, tools, cancel, cb, max_tokens):
    url = cfg["base"].rstrip("/") + "/v1/messages"
    conv = _an_messages(messages)
    if conv and conv[-1]["content"]:
        conv[-1]["content"][-1] = dict(conv[-1]["content"][-1], cache_control={"type": "ephemeral"})
    body = {"model": cfg["model"], "max_tokens": max_tokens or DEFAULT_MAX_TOKENS, "stream": True, "messages": conv}
    if system:
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                         for t in tools]
    body.update(cfg.get("extra_body") or {})
    resp = _open(cfg, url, body, cancel, cb.get("retry"))
    text, blocks, usage, finish = [], {}, {}, ""
    try:
        for ev, data in _sse(resp, cancel):
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            t = obj.get("type") or ev
            if t == "error":
                e = obj.get("error") or {}
                raise ProviderError(scrub(f"{cfg['name']}: {e.get('message', 'error')}", [cfg.get("api_key") or ""])[:400],
                                    retryable=e.get("type") == "overloaded_error")
            if t == "message_start":
                usage.update((obj.get("message") or {}).get("usage") or {})
            elif t == "content_block_start":
                b = obj.get("content_block") or {}
                blocks[obj.get("index", 0)] = {"type": b.get("type"), "id": b.get("id", ""), "name": b.get("name", ""),
                                               "json": ""}
            elif t == "content_block_delta":
                d = obj.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta":
                    text.append(d.get("text", ""))
                    cb["text"](d.get("text", ""))
                elif dt == "thinking_delta":
                    cb["think"](d.get("thinking", ""))
                elif dt == "input_json_delta":
                    blocks.setdefault(obj.get("index", 0), {"type": "tool_use", "id": "", "name": "", "json": ""})
                    blocks[obj.get("index", 0)]["json"] += d.get("partial_json", "")
            elif t == "message_delta":
                finish = (obj.get("delta") or {}).get("stop_reason") or finish
                usage.update(obj.get("usage") or {})
    finally:
        resp.close()
    tool_calls = []
    for i in sorted(blocks):
        b = blocks[i]
        if b["type"] != "tool_use":
            continue
        try:
            args = json.loads(b["json"]) if b["json"].strip() else {}
            bad = "" if isinstance(args, dict) else "the tool arguments were not an object"
            args = args if isinstance(args, dict) else {}
        except ValueError as e:
            args, bad = {}, f"the tool arguments were not valid JSON ({e})"
        tool_calls.append({"id": b["id"], "name": b["name"], "args": args, "bad": bad})
    cached = int(usage.get("cache_read_input_tokens") or 0)
    u = {"input": int(usage.get("input_tokens") or 0) + cached + int(usage.get("cache_creation_input_tokens") or 0),
         "output": int(usage.get("output_tokens") or 0), "cached": cached}
    return {"content": "".join(text), "tool_calls": tool_calls, "usage": u, "finish": finish}


def stream_chat(cfg, system, messages, tools, *, cancel=None, on_text=None, on_think=None, on_retry=None,
                max_tokens=None):
    """One model call. Returns ``{content, tool_calls, usage, finish}``; callbacks fire while it streams."""
    cb = {"text": on_text or (lambda s: None), "think": on_think or (lambda s: None), "retry": on_retry}
    mt = max_tokens or cfg.get("max_tokens") or (DEFAULT_MAX_TOKENS if cfg["kind"] == "anthropic" else None)
    try:
        if cfg["kind"] == "anthropic":
            return _stream_anthropic(cfg, system, messages, tools, cancel, cb, mt)
        return _stream_openai(cfg, system, messages, tools, cancel, cb, mt)
    except requests.RequestException as e:
        raise ProviderError(scrub(f"connection to {cfg['name']} dropped: {e.__class__.__name__}",
                                  [cfg.get("api_key") or ""]), retryable=True)


def complete(cfg, system, prompt, max_tokens=700, cancel=None):
    """A plain, tool-less completion (used for summaries). Returns the text."""
    res = stream_chat(cfg, system, [{"role": "user", "content": prompt}], [], cancel=cancel, max_tokens=max_tokens)
    return res["content"]


# ------------------------------------------------------------------------------------------- models
def list_models(cfg, ttl=600):
    """Model ids the provider offers (cached for ``ttl`` seconds); raises :class:`ProviderError`."""
    ck = (cfg["base"], hashlib.sha1((cfg.get("api_key") or "").encode()).hexdigest()[:10])
    now = time.time()
    with _lock:
        hit = _models_cache.get(ck)
        if hit and now - hit[0] < ttl:
            return hit[1]
    h = _headers(cfg)
    h.pop("Accept", None)
    url = cfg["base"].rstrip("/") + "/models" if cfg["kind"] != "anthropic" else cfg["base"].rstrip("/") + "/v1/models?limit=100"
    try:
        r = requests.get(url, headers=h, timeout=(CONNECT_TIMEOUT, 30))
    except requests.RequestException as e:
        raise ProviderError(scrub(f"cannot reach {cfg['name']}: {e.__class__.__name__}", [cfg.get("api_key") or ""]))
    if r.status_code >= 400:
        raise ProviderError(scrub(_explain(r.status_code, r.text, cfg), [cfg.get("api_key") or ""]), status=r.status_code)
    try:
        j = r.json()
    except ValueError:
        raise ProviderError(f"{cfg['name']} did not return a model list")
    items = j.get("data") if isinstance(j, dict) else j
    ids = sorted({(m.get("id") or m.get("name") or "") if isinstance(m, dict) else str(m) for m in items or []} - {""})
    with _lock:
        _models_cache[ck] = (now, ids)
    return ids


def forget_models(cfg):
    """Drop the cached model list of this provider so the next listing asks the server again."""
    ck = (cfg["base"], hashlib.sha1((cfg.get("api_key") or "").encode()).hexdigest()[:10])
    with _lock:
        _models_cache.pop(ck, None)


def test(cfg):
    """Check a provider end to end. Returns ``{ok, ms, message, models}`` (never raises, never has a key)."""
    t0 = time.time()
    try:
        ids = []
        try:
            ids = list_models(cfg, ttl=0)
        except ProviderError as e:
            if e.status in (401, 403):
                raise
        res = stream_chat(cfg, "", [{"role": "user", "content": "Reply with the single word: ok"}], [], max_tokens=16)
        return {"ok": True, "ms": int((time.time() - t0) * 1000), "models": len(ids),
                "message": (res["content"] or "(empty reply)").strip()[:80]}
    except ProviderError as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "message": str(e), "models": 0}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "message": f"{e.__class__.__name__}: {e}", "models": 0}
