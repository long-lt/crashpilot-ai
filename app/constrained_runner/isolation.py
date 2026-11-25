"""Least-privilege child process for the supported macOS desktop runner."""
from contextlib import contextmanager
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


@contextmanager
def isolated_command(command, repo, *, writable, inputs, variables, credential_names=(), proxy_port=None, listen_port=None):
    sandbox = Path("/usr/bin/sandbox-exec")
    if sys.platform != "darwin" or not sandbox.is_file():
        raise RuntimeError("Không thể chạy agent khi thiếu cơ chế cách ly macOS.")
    repo = Path(repo).resolve()
    executable = shutil.which(command[0])
    if not executable:
        raise FileNotFoundError(command[0])
    with tempfile.TemporaryDirectory(prefix="crashpilot-agent-") as scratch:
        private = Path(scratch).resolve()
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", *credential_names) if k in os.environ}
        env.update(variables, HOME=str(private), TMPDIR=str(private), PYTHONDONTWRITEBYTECODE="1",
                   GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1")
        # Read access to a configured script/binary does not grant access to its parent directory.
        resolved_exec = Path(executable).resolve()
        files = [Path(executable), resolved_exec, *inputs, *(Path(arg) for arg in command[1:] if arg.startswith("/") and len(arg) < 1024)]
        system = ["/usr", "/bin", "/sbin", "/System/Library", "/Library/Apple", "/Library/Developer", "/Library/Frameworks", "/Library/Preferences", "/opt/homebrew/bin", "/opt/homebrew/share", "/opt/homebrew/Cellar", "/opt/homebrew/lib", "/opt/homebrew/etc", "/private/etc/ssl", sys.base_prefix]
        for xcode_dir in (Path("/Applications/Xcode.app"), Path("/private/var/db/xcode_select_link")):
            if xcode_dir.exists():
                system.append(str(xcode_dir))
        home = Path.home().resolve()
        for tool_dir in (
            home / "Development/flutter",
            home / ".pub-cache",
        ):
            if tool_dir.exists():
                system.append(str(tool_dir))
        exec_root = resolved_exec.parent.parent
        if exec_root.is_dir() and exec_root not in {Path("/"), Path("/Users"), home, Path("/tmp"), Path("/private/tmp"), Path("/private/var")}:
            if not str(exec_root).startswith("/private/var/folders/"):
                system.append(str(exec_root))
        git_link = repo / ".git"
        cli_names = {"claude", "codex", "gemini", "opencode", "agy"}
        cmd_name = Path(command[0]).name.lower()
        exec_name = resolved_exec.name.lower()
        cli_id = cmd_name if cmd_name in cli_names else (exec_name if exec_name in cli_names else None)
        if cmd_name in {"flutter", "dart"}:
            android_sdk = Path(os.environ.get("ANDROID_HOME") or home / "Library/Android/sdk")
            if cmd_name == "flutter" and android_sdk.is_dir():
                env["ANDROID_HOME"] = str(android_sdk)
                system.append(str(android_sdk))
            env["PUB_CACHE"] = str(home / ".pub-cache")
            env["FLUTTER_SUPPRESS_ANALYTICS"] = "true"
        is_cli_agent = cli_id is not None
        cli_write_dirs: list[Path] = []
        deny_writes: list[Path] = [git_link]
        if is_cli_agent:
            for k in ("USER", "LOGNAME"):
                if k in os.environ:
                    env[k] = os.environ[k]
            for kc in (Path("/Library/Keychains"), home / "Library/Keychains"):
                if kc.exists():
                    system.append(str(kc))
            user_kc = home / "Library/Keychains"
            if user_kc.is_dir():
                dst_kc = private / "Library/Keychains"
                dst_kc.parent.mkdir(parents=True, exist_ok=True)
                if not dst_kc.exists():
                    dst_kc.symlink_to(user_kc)
            if cli_id == "agy":
                # The CLI rewrites this identifier atomically after a successful Keychain login.
                # Keep those writes in the per-invocation home, away from the IDE profile.
                scope_rel = Path("Library/Application Support/antigravity/credential-user-scope")
                scope_dest = private / scope_rel
                scope_dest.parent.mkdir(parents=True, exist_ok=True)
                if (home / scope_rel).is_file():
                    shutil.copy2(home / scope_rel, scope_dest)
            uid = os.getuid()
            p_tmp = Path(f"/private/tmp/{cli_id}-{uid}")
            p_tmp.mkdir(parents=True, exist_ok=True)
            system.append(str(p_tmp))
            cli_write_dirs.append(p_tmp)
            if cli_id == "codex":
                if (home / ".codex-cli-account").is_dir():
                    env["CODEX_HOME"] = str(home / ".codex-cli-account")
                elif (home / ".codex").is_dir():
                    env["CODEX_HOME"] = str(home / ".codex")
            if cli_id == "gemini":
                env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"

            # Cấp quyền tối thiểu đúng theo từng CLI Agent (tuyệt đối không mở toàn bộ ~/.config hay ~/.local/share)
            agent_cfg_paths: dict[str, tuple[str, ...]] = {
                "claude": (".claude", ".claude.json"),
                "codex": (".codex-cli-account", ".codex"),
                "gemini": (".gemini",),
                "agy": (".gemini", ".agy"),
                "opencode": (".opencode", ".config/opencode", ".local/share/opencode"),
            }
            for cfg_rel in agent_cfg_paths.get(cli_id, ()):
                src_cfg = home / cfg_rel
                if src_cfg.is_dir():
                    system.append(str(src_cfg))
                    cli_write_dirs.append(src_cfg)
                    dst_cfg = private / cfg_rel
                    if cli_id == "agy" and cfg_rel == ".gemini":
                        # Stage ~/.gemini without external GUI MCP servers (blender/browser-use/stitch)
                        # so headless agy starts in ~1s instead of downloading uvx/npx packages into temp HOME.
                        dst_cfg.mkdir(parents=True, exist_ok=True)
                        for child in src_cfg.iterdir():
                            if child.name == "antigravity-cli" and child.is_dir():
                                cli_dst = dst_cfg / "antigravity-cli"
                                cli_dst.mkdir(parents=True, exist_ok=True)
                                for sub in child.iterdir():
                                    if sub.name in {"mcp_config.json", "mcp"}:
                                        continue
                                    (cli_dst / sub.name).symlink_to(sub)
                                (cli_dst / "mcp_config.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
                            else:
                                (dst_cfg / child.name).symlink_to(child)
                    else:
                        dst_cfg.parent.mkdir(parents=True, exist_ok=True)
                        if not dst_cfg.exists():
                            dst_cfg.symlink_to(src_cfg)
                elif src_cfg.is_file():
                    files.append(src_cfg)
                    dst_file = private / cfg_rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_cfg, dst_file)

            # Chặn ghi đè lên các file cấu hình, hooks, skills, plugins, MCP của host agent (chống prompt-injection leo thang)
            for protected_rel in (
                ".claude/settings.json",
                ".claude/settings.local.json",
                ".claude/commands",
                ".claude/skills",
                ".claude/agents",
                ".codex/config.toml",
                ".codex-cli-account/config.toml",
                ".gemini/settings.json",
                ".gemini/config",
                ".gemini/antigravity/builtin",
                ".gemini/antigravity/mcp_config.json",
                ".gemini/antigravity-cli/builtin",
                ".gemini/antigravity-cli/mcp_config.json",
                ".gemini/antigravity-cli/settings.json",
                ".config/opencode/opencode.json",
            ):
                deny_writes.append(home / protected_rel)
        files.extend(Path("/private/etc") / name for name in ("hosts", "localtime", "services", "protocols", "resolv.conf", "mime.types", "apache2/mime.types", "ssl/cert.pem"))
        venv = Path(executable).parent.parent
        if (venv / "pyvenv.cfg").is_file():
            files.append(venv / "pyvenv.cfg")
            system.append(str(venv / "lib"))
        allow_read = [f'(subpath {json.dumps(str(Path(path).resolve()))})' for path in [*system, repo, private]]
        allow_read.extend(f'(literal {json.dumps(str(path.resolve()))})' for path in files if path.is_file())
        # Flutter wrappers run git inside the SDK to resolve its engine revision.
        # A task-specific GIT_DIR would make them use the app commit as the SDK revision.
        if git_link.is_file() and cmd_name not in {"flutter", "dart"}:
            target = git_link.read_text().removeprefix("gitdir: ").strip()
            git_dir = (repo / target).resolve()
            common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
            objects = common / "objects"
            allow_read.append(f'(subpath {json.dumps(str(objects))})')
            # Give Git its own metadata and index; the owner's config, hooks and refs stay private.
            local_git = private / "git"
            subprocess.run(["git", "init", "--bare", "--template=", str(local_git)], check=True,
                           capture_output=True, timeout=10)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True, timeout=10)
            (local_git / "HEAD").write_text(head.strip() + "\n")
            if (git_dir / "index").is_file():
                shutil.copyfile(git_dir / "index", local_git / "index")
            env.update(GIT_DIR=str(local_git), GIT_WORK_TREE=str(repo),
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=str(objects))
        writes = [private, repo, *cli_write_dirs] if writable else [private, *cli_write_dirs]
        if writable and cmd_name == "flutter" and list(command[1:3]) == ["pub", "get"]:
            # Pub registers the worktree here even when all packages are cached.
            writes.append(home / ".pub-cache/active_roots")
        flutter_cache = home / "Development/flutter/bin/cache"
        if writable and cmd_name in {"flutter", "dart"} and flutter_cache.is_dir():
            writes.append(flutter_cache)
        allow_write = " ".join(f'(subpath {json.dumps(str(p))})' for p in writes)
        deny_clauses = [f'(literal {json.dumps(str(git_link))})']
        for dp in deny_writes[1:]:
            resolved_dp = dp.resolve() if dp.exists() else dp
            deny_clauses.append(f'(subpath {json.dumps(str(resolved_dp))})' if resolved_dp.is_dir() else f'(literal {json.dumps(str(resolved_dp))})')
        network = ""
        if proxy_port is not None:
            if not 1 <= proxy_port <= 65535:
                raise ValueError("Invalid loopback proxy port")
            env["HTTPS_PROXY"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
            network = f'(allow network-outbound (remote tcp "localhost:{proxy_port}"))'
            if is_cli_agent:
                network += '(allow network-bind network-inbound (local tcp "localhost:*"))(allow network-outbound (remote tcp "localhost:*"))(allow mach-lookup)'
        elif is_cli_agent:
            network = '(allow network-outbound)(allow network-bind network-inbound (local tcp "localhost:*"))(allow network* (local unix))(allow mach-lookup)'
        elif cmd_name in {"flutter", "dart"}:
            # Dart VM service / flutter_tester / xcrun native-assets need local loopback, unix sockets & mach lookup, without external internet.
            network = '(allow network-bind network-inbound (local tcp "localhost:*"))(allow network-outbound (remote tcp "localhost:*"))(allow network* (local unix))(allow mach-lookup)'
        if listen_port is not None:
            network += f'(allow network-bind network-inbound (local tcp "localhost:{int(listen_port)}"))'
        xcrun_cache = ' (regex #"^/private/var/folders/.+/T/xcrun_db[^/]*$")' if (is_cli_agent or cmd_name in {"flutter", "dart"}) else ""
        pty_rules = "(allow pseudo-tty)" if is_cli_agent else ""
        dev_write = '(literal "/dev/null") (literal "/dev/ptmx") (regex #"^/dev/ttys[0-9]+$")' if is_cli_agent else '(literal "/dev/null")'
        profile = (f'(version 1)(deny default)(import "dyld-support.sb")(allow process-exec process-fork sysctl-read file-read-metadata syscall* mach-bootstrap dynamic-code-generation ipc-posix-shm* ipc-posix-sem* system-socket)(allow signal (target same-sandbox)){pty_rules}'
                   f'(allow file-read* file-map-executable {" ".join(allow_read)}{xcrun_cache} (subpath "/dev"))'
                   f'(allow file-write* {allow_write}{xcrun_cache} {dev_write})'
                   f'(deny file-write* {" ".join(deny_clauses)}){network}')
        yield [str(sandbox), "-p", profile, executable, *command[1:]], env


def run_agent(command, repo, *, writable, inputs, variables, timeout, credential_names=(), proxy_port=None):
    with isolated_command(command, repo, writable=writable, inputs=inputs, variables=variables,
                          credential_names=credential_names, proxy_port=proxy_port) as (argv, env):
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            with subprocess.Popen(argv, cwd=repo, env=env,
                                  stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f,
                                  start_new_session=True) as proc:
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait()
                    out_f.seek(0)
                    err_f.seek(0)
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout,
                        output=out_f.read().decode("utf-8", errors="replace"),
                        stderr=err_f.read().decode("utf-8", errors="replace"),
                    )
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                out_f.seek(0)
                err_f.seek(0)
                stdout = out_f.read().decode("utf-8", errors="replace")
                stderr = err_f.read().decode("utf-8", errors="replace")
                return subprocess.CompletedProcess(command, rc, stdout, stderr)
