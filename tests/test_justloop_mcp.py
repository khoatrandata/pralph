"""justloop --no-mcp / --mcp-config: which MCP servers iterations start with."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import pralph.loop as loop
import pralph.runner as runner
from pralph.cli import main
from pralph.runner import ClaudeResult


def _cmd(monkeypatch, **kwargs):
    seen = []

    def fake_popen(cmd, **kw):
        seen.append(cmd)
        raise FileNotFoundError

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    runner.run_claude("hi", **kwargs)
    return seen[0]


class TestRunnerArgs:
    def test_default_leaves_mcp_alone(self, monkeypatch):
        cmd = _cmd(monkeypatch)
        assert "--strict-mcp-config" not in cmd and "--mcp-config" not in cmd

    def test_no_mcp(self, monkeypatch):
        cmd = _cmd(monkeypatch, mcp_configs=[])
        i = cmd.index("--mcp-config")
        assert "--strict-mcp-config" in cmd
        assert json.loads(cmd[i + 1]) == {"mcpServers": {}}

    def test_keep_listed_configs(self, monkeypatch):
        cmd = _cmd(monkeypatch, mcp_configs=["/a.json", "/b.json"])
        i = cmd.index("--mcp-config")
        assert cmd[i + 1:i + 3] == ["/a.json", "/b.json"]

    def test_resume_too(self, monkeypatch):
        cmd = _cmd(monkeypatch, resume_session_id="s1", mcp_configs=[])
        assert "--resume" in cmd and "--strict-mcp-config" in cmd


def _nothing_left():
    return ClaudeResult(success=True, cost_usd=0.01, structured_output={
        "step": "", "status": "nothing_left", "commit": "", "note": "", "blocker": "", "needs_human": []})


class TestLoop:
    @pytest.mark.parametrize("configs", [None, [], ["/x.json"]])
    def test_passed_to_each_iteration(self, jsonl_state, monkeypatch, configs):
        seen = []
        monkeypatch.setattr(loop, "run_with_retry", lambda prompt, **kw: seen.append(kw) or _nothing_left())
        loop.run_justloop(jsonl_state, user_prompt="x", max_iterations=1, cooldown=0, mcp_configs=configs)
        assert seen[0]["mcp_configs"] == configs

    def test_cli(self, jsonl_state, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr("pralph.cli.run_justloop", lambda state, **kw: seen.update(kw))
        d = str(jsonl_state.project_dir)
        r = CliRunner().invoke(main, ["--project-dir", d, "justloop", "--no-mcp", "go"])
        assert r.exit_code == 0 and seen["mcp_configs"] == []
        cfg = tmp_path / "mcp.json"
        cfg.write_text('{"mcpServers": {}}')
        CliRunner().invoke(main, ["--project-dir", d, "justloop", "--mcp-config", str(cfg), "go"])
        assert seen["mcp_configs"] == [str(cfg)]
        CliRunner().invoke(main, ["--project-dir", d, "justloop", "go"])
        assert seen["mcp_configs"] is None
        r = CliRunner().invoke(main, ["--project-dir", d, "justloop", "--no-mcp", "--mcp-config", str(cfg), "go"])
        assert r.exit_code == 2 and "not both" in r.output
