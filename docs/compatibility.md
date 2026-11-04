# Compatibility

What Rconsole is tested on and what it needs. "Tested" means the automated suite and the CI image build; hosting
platforms themselves are not tested in CI.

## Runtime

| Requirement | Value |
|---|---|
| Python | **3.14 or newer** (`requires-python` in `pyproject.toml`, the `python:3.14-slim` Docker image, CI) |
| Dependencies | [`uv`](https://docs.astral.sh/uv/) (`uv sync`), or `pip install -r requirements.txt` |
| Browser | Any current browser with WebSocket support; installable as an app on phones |

Older Python versions are not supported.

## Operating systems

| Host OS | Console, editor, file manager | Linux systems (`vm`) | Notes |
|---|:---:|:---:|---|
| **Linux** | Yes | Yes | The supported production target. gunicorn is used. |
| **macOS** | Yes | No | Run with `uv run python serve.py`. |
| **Windows** | Yes | No | ConPTY via `pywinpty`; gunicorn does not exist there, so `serve.py` uses its built-in threaded server. |
| **Android (Termux)** | Untested | Untested | `pkg install python git proot` works only if Termux provides Python 3.14+. Check `python --version`. Android may kill background processes, which stops service supervision. Local use only; use Tailscale or a tunnel to reach it. |

## CPU architectures

| | x86-64 | arm64 (Raspberry Pi 4/5, Graviton, Oracle Ampere) |
|---|---|---|
| Docker image | Yes (multi-arch image on GHCR) | Yes |
| Linux systems | Yes: Rconsole downloads a checksum-verified **PRoot 5.5.0** itself | Yes, **if you provide PRoot 5.5.0 or newer** (see below) |
| Alpine, Debian, Ubuntu, Kali images | amd64 / x86_64 | arm64 / aarch64 |

### PRoot

* Rconsole needs **PRoot 5.5.0 or newer**. Older builds (for example the one in many distro packages) cannot run the
  Rust-based coreutils of Ubuntu 26.04 (`coreutils: unknown program 'prooted-…'`).
* On x86-64 the pinned 5.5.0 is downloaded on first use. On other CPUs there is no download: install PRoot yourself
  and point `RCONSOLE_VM_PROOT` at it. The Docker image installs the distribution's PRoot, **which may be older than
  5.5.0**; check with `proot --version`, and set `RCONSOLE_VM_PROOT` to a newer build if `vm doctor` reports a problem.
* PRoot needs `ptrace`. Hosts that block it cannot run Linux systems; `vm install` reports why.

### PID namespaces

Process isolation between systems needs unprivileged **user namespaces** (`unshare --user --pid`, `nsenter`). Docker's
default seccomp profile blocks them; run with `--security-opt seccomp=unconfined` if you want isolation. `vm doctor`
shows whether you have it.

## Hosting platforms

See the [provider table](../DEPLOY.md#2-choose-a-platform) for what each platform offers, with the date it was last
checked. Free tiers change often; verify against the provider's current terms.
