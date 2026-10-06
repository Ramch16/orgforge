
import pytest

from orgforge import engines, machine, platforms
from orgforge.engines import BUILTIN_ENGINES, EngineError

NPM_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
'''


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platforms, "WINDOWS", True)


def test_npm_wrappers_are_bypassed_so_cmd_never_reads_the_prompt(windows, tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    shim.write_text(NPM_SHIM)
    script = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// codex")
    monkeypatch.setattr(platforms.shutil, "which", lambda b: {"codex": str(shim), "node": "/x/node.exe"}.get(b))
    argv = platforms.safe_argv(["codex", "exec", "--sandbox", "read-only", 'say "hi" & del *'])
    assert argv == ["/x/node.exe", str(script.resolve()), "exec", "--sandbox", "read-only", 'say "hi" & del *']


def test_other_batch_files_are_refused_when_arguments_could_inject(windows, tmp_path, monkeypatch):
    bat = tmp_path / "tool.bat"
    bat.write_text("@echo off\nrealtool.exe %*\n")
    monkeypatch.setattr(platforms.shutil, "which", lambda b: str(bat) if b == "tool" else None)
    assert platforms.safe_argv(["tool", "--model", "small"]) == [str(bat), "--model", "small"]
    for bad in ["a & calc", "a | b", "100%", "line\nbreak", 'quote"d', "^x", "(a)"]:
        with pytest.raises(platforms.UnsafeCommand, match="batch wrapper"):
            platforms.safe_argv(["tool", "-p", bad])


def test_real_executables_are_started_directly(windows, monkeypatch):
    monkeypatch.setattr(platforms.shutil, "which", lambda b: r"C:\Tools\claude.exe" if b == "claude" else None)
    assert platforms.safe_argv(["claude", "-p", "a & b"]) == [r"C:\Tools\claude.exe", "-p", "a & b"]


def test_nothing_changes_off_windows(monkeypatch):
    monkeypatch.setattr(platforms, "WINDOWS", False)
    assert platforms.safe_argv(["codex", "a & b"]) == ["codex", "a & b"]
    assert platforms.shell_command("npm test && echo ok") == ("npm test && echo ok", True)


def test_commands_run_in_git_bash_never_wsl(windows, tmp_path, monkeypatch):
    git_root = tmp_path / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir()
    (git_root / "cmd" / "git.exe").write_text("")
    (git_root / "bin" / "bash.exe").write_text("")
    monkeypatch.delenv("ORGFORGE_BASH", raising=False)
    monkeypatch.setattr(platforms.shutil, "which", lambda b: str(git_root / "cmd" / "git.exe") if b == "git" else None)
    bash = str((git_root / "bin" / "bash.exe").resolve())
    assert platforms.shell_command("python -m pytest -q") == ([bash, "-lc", "python -m pytest -q"], False)
    wsl = tmp_path / "Windows" / "System32"
    wsl.mkdir(parents=True)
    (wsl / "git.exe").write_text("")
    (wsl / "bash.exe").write_text("")
    monkeypatch.setattr(platforms.shutil, "which", lambda b: str(wsl / "git.exe") if b == "git" else None)
    assert platforms.git_bash() is None
    assert platforms.shell_command("dir") == ("dir", True)                        # cmd.exe as a last resort


def test_a_refused_cli_is_an_engine_error_not_a_crash(windows, tmp_path, monkeypatch):
    bat = tmp_path / "gemini.bat"
    bat.write_text("@echo off\n")
    monkeypatch.setattr(platforms.shutil, "which", lambda b: str(bat) if b == "gemini" else None)
    with pytest.raises(EngineError, match="batch wrapper"):
        engines.run_cli(BUILTIN_ENGINES["gemini"], system="rules", prompt="Fix T-1 & ship", cwd=str(tmp_path),
                        model="", write=False, run=False, timeout=10)


def test_store_shortcuts_are_not_python(monkeypatch):
    monkeypatch.setattr(machine, "system", lambda: "windows")
    stub = r"C:\Users\ram\AppData\Local\Microsoft\WindowsApps\python.exe"
    monkeypatch.setattr(machine.shutil, "which", lambda b: stub if b == "python" else None)
    monkeypatch.setattr(machine, "_run", lambda argv, timeout=15: (9009, ""))
    status = machine.check_tool("python")
    assert status["state"] == "missing" and "Microsoft Store shortcut" in status["detail"]
    assert "winget install --id Python.Python.3.12 -e" in status["detail"]
