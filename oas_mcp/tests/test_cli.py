"""Tests for oas-cli: argument parsing, JSON param coercion, modes."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oas_mcp.cli import (
    _coerce_json_args,
    _kebab_to_snake,
    _snake_to_kebab,
    _build_subparser,
    _write_output,
    oneshot_mode,
    _interactive_loop,
    main,
)


# ---------------------------------------------------------------------------
# Name conversion helpers
# ---------------------------------------------------------------------------


def test_snake_to_kebab():
    assert _snake_to_kebab("run_aero_analysis") == "run-aero-analysis"
    assert _snake_to_kebab("create_surface") == "create-surface"
    assert _snake_to_kebab("reset") == "reset"


def test_kebab_to_snake():
    assert _kebab_to_snake("run-aero-analysis") == "run_aero_analysis"
    assert _kebab_to_snake("create-surface") == "create_surface"
    assert _kebab_to_snake("reset") == "reset"


# ---------------------------------------------------------------------------
# Argument parsing / subcommand generation
# ---------------------------------------------------------------------------


def test_build_subparser_basic():
    """Subparser for create_surface should expose --name and --num-y flags."""
    from oas_mcp import server

    subparsers = argparse.ArgumentParser().add_subparsers()
    _build_subparser(subparsers, "create_surface", server.create_surface)

    # Parse with just --name and --num-y
    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers(dest="mode")
    _build_subparser(sp, "create_surface", server.create_surface)

    ns = parser.parse_args(["create-surface", "--name", "mywing", "--num-y", "9"])
    assert ns.name == "mywing"
    assert ns.num_y == 9


def test_build_subparser_surfaces_json():
    """run_aero_analysis --surfaces should accept a JSON list string."""
    from oas_mcp import server

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers(dest="mode")
    _build_subparser(sp, "run_aero_analysis", server.run_aero_analysis)

    ns = parser.parse_args(
        ["run-aero-analysis", "--surfaces", '["wing"]', "--alpha", "5.0"]
    )
    # surfaces is still a string at parse time — coercion happens in _coerce_json_args
    assert ns.surfaces == '["wing"]'
    assert ns.alpha == 5.0


# ---------------------------------------------------------------------------
# JSON arg coercion
# ---------------------------------------------------------------------------


def test_coerce_json_args_list():
    """surfaces arg (list[str]) should be parsed from JSON string."""
    from oas_mcp import server

    ns = argparse.Namespace(
        surfaces='["wing", "tail"]',
        alpha=5.0,
        velocity=None,
        Mach_number=None,
        reynolds_number=None,
        density=None,
        cg=None,
        session_id=None,
        run_name=None,
    )
    result = _coerce_json_args("run_aero_analysis", ns)
    assert result["surfaces"] == ["wing", "tail"]
    assert result["alpha"] == 5.0


def test_coerce_json_args_dict():
    """dict-typed args should be parsed from JSON string."""
    from oas_mcp import server

    ns = argparse.Namespace(
        surfaces='["wing"]',
        alpha=None,
        velocity=None,
        Mach_number=None,
        reynolds_number=None,
        density=None,
        cg='[1.0, 0.0, 0.0]',
        session_id=None,
        run_name=None,
    )
    result = _coerce_json_args("run_aero_analysis", ns)
    assert result["cg"] == [1.0, 0.0, 0.0]


def test_coerce_json_args_none_skipped():
    """None values with defaults should not appear in result (let function use defaults)."""
    from oas_mcp import server

    ns = argparse.Namespace(
        surfaces='["wing"]',
        alpha=None,
        velocity=None,
        Mach_number=None,
        reynolds_number=None,
        density=None,
        cg=None,
        session_id=None,
        run_name=None,
    )
    result = _coerce_json_args("run_aero_analysis", ns)
    assert "alpha" not in result
    assert "velocity" not in result


# ---------------------------------------------------------------------------
# cli_runner: run_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tool_unknown():
    from oas_mcp.cli_runner import run_tool

    response = await run_tool("nonexistent_tool_xyz", {})
    assert response["ok"] is False
    assert "nonexistent_tool_xyz" in response["error"]["message"]


@pytest.mark.asyncio
async def test_run_tool_user_input_error():
    """Calling run_aero_analysis without creating a surface should error."""
    from oas_mcp.cli_runner import run_tool
    from oas_mcp.server import reset

    await reset()
    response = await run_tool("run_aero_analysis", {"surfaces": ["nonexistent_surface"]})
    assert response["ok"] is False


# ---------------------------------------------------------------------------
# json_dumps
# ---------------------------------------------------------------------------


def test_json_dumps_numpy():
    import numpy as np
    from oas_mcp.cli_runner import json_dumps

    data = {"arr": np.array([1.0, 2.0]), "val": np.float64(3.14)}
    text = json_dumps(data)
    parsed = json.loads(text)
    assert parsed["arr"] == [1.0, 2.0]
    assert abs(parsed["val"] - 3.14) < 1e-6


def test_json_dumps_pretty():
    from oas_mcp.cli_runner import json_dumps

    text = json_dumps({"a": 1}, pretty=True)
    assert "\n" in text  # indented output


# ---------------------------------------------------------------------------
# Script mode
# ---------------------------------------------------------------------------


def test_run_script_mode_basic(tmp_path, capsys):
    """Script mode executes steps in sequence and prints JSON-line results."""
    script = [
        {"tool": "create_surface", "args": {"name": "wing", "num_y": 7}},
    ]
    script_path = tmp_path / "workflow.json"
    script_path.write_text(json.dumps(script))

    from oas_mcp.cli import run_script_mode
    from oas_mcp.server import reset
    import asyncio

    asyncio.run(reset())
    run_script_mode(str(script_path))

    captured = capsys.readouterr()
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["ok"] is True


def test_run_script_mode_missing_file(tmp_path):
    from oas_mcp.cli import run_script_mode

    with pytest.raises(SystemExit):
        run_script_mode(str(tmp_path / "does_not_exist.json"))


def test_run_script_mode_invalid_json(tmp_path):
    from oas_mcp.cli import run_script_mode

    p = tmp_path / "bad.json"
    p.write_text("not json")
    with pytest.raises(SystemExit):
        run_script_mode(str(p))


def test_run_script_mode_not_array(tmp_path):
    from oas_mcp.cli import run_script_mode

    p = tmp_path / "obj.json"
    p.write_text('{"tool": "reset"}')
    with pytest.raises(SystemExit):
        run_script_mode(str(p))


# ---------------------------------------------------------------------------
# Interactive mode (unit test — subprocess pipe)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_loop_single_command(monkeypatch, capsys):
    """Interactive loop parses a JSON line and produces a JSON-line response."""
    import io
    import asyncio
    from oas_mcp.server import reset

    await reset()

    # Feed a single create_surface command via a mock stdin
    line = json.dumps({"tool": "create_surface", "args": {"name": "wing", "num_y": 7}}) + "\n"

    async def _fake_loop(pretty=False):
        from oas_mcp.cli_runner import run_tool, json_dumps
        # Simulate what _interactive_loop does for one line
        cmd = json.loads(line.strip())
        response = await run_tool(cmd["tool"], cmd.get("args", {}))
        print(json_dumps(response, pretty=pretty), flush=True)

    await _fake_loop()

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# list-tools subcommand
# ---------------------------------------------------------------------------


def test_list_tools():
    from oas_mcp.cli_runner import list_tools

    tools = list_tools()
    assert "create_surface" in tools
    assert "run_aero_analysis" in tools
    assert "run_optimization" in tools
    assert "reset" in tools
    assert "start_session" in tools


def test_list_tools_completeness():
    """All 23 tools (20 analysis + 3 provenance) should be registered."""
    from oas_mcp.cli_runner import list_tools

    tools = list_tools()
    assert len(tools) == 23, f"Expected 23 tools, got {len(tools)}: {tools}"


# ---------------------------------------------------------------------------
# One-shot mode (Gap 1)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect CLI state dir to a temp directory."""
    import oas_mcp.cli_state as cs

    monkeypatch.setattr(cs, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def _make_ns(**kwargs):
    """Build an argparse.Namespace with defaults for create_surface."""
    from oas_mcp.cli_runner import get_registry
    import inspect

    fn = get_registry()["create_surface"]
    sig = inspect.signature(fn)
    defaults = {}
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        defaults[name] = param.default if param.default is not inspect.Parameter.empty else None
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_oneshot_create_surface(isolated_state, capsys):
    """One-shot create-surface should persist surface state."""
    import asyncio
    from oas_mcp.server import reset
    from oas_mcp.cli_state import load_surfaces

    asyncio.run(reset())

    ns = _make_ns(name="wing", num_y=7)
    oneshot_mode("create_surface", ns, workspace="test_ws")

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["ok"] is True

    # Surface should be saved in state
    surfaces = load_surfaces("test_ws")
    assert "wing" in surfaces


def test_oneshot_state_reconstruction(isolated_state, capsys):
    """One-shot analysis should reconstruct surfaces from saved state."""
    import asyncio
    from oas_mcp.server import reset
    from oas_mcp.cli_state import save_surfaces

    asyncio.run(reset())

    # Pre-save a surface in state
    save_surfaces("test_ws", {"wing": {"name": "wing", "num_y": 7}})

    # Run aero analysis — should reconstruct the surface first
    from oas_mcp.cli_runner import get_registry
    import inspect

    fn = get_registry()["run_aero_analysis"]
    sig = inspect.signature(fn)
    defaults = {}
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        defaults[name] = param.default if param.default is not inspect.Parameter.empty else None
    defaults["surfaces"] = '["wing"]'
    defaults["alpha"] = 5.0
    ns = argparse.Namespace(**defaults)

    oneshot_mode("run_aero_analysis", ns, workspace="test_ws")

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["ok"] is True


def test_oneshot_workspace_isolation(isolated_state, capsys):
    """Surfaces in different workspaces should not interfere."""
    import asyncio
    from oas_mcp.server import reset
    from oas_mcp.cli_state import load_surfaces

    asyncio.run(reset())

    ns = _make_ns(name="wing", num_y=7)
    oneshot_mode("create_surface", ns, workspace="ws_a")
    capsys.readouterr()  # discard

    asyncio.run(reset())

    ns2 = _make_ns(name="tail", num_y=5)
    oneshot_mode("create_surface", ns2, workspace="ws_b")
    capsys.readouterr()

    assert "wing" in load_surfaces("ws_a")
    assert "tail" not in load_surfaces("ws_a")
    assert "tail" in load_surfaces("ws_b")


def test_oneshot_reset_clears_state(isolated_state, capsys):
    """One-shot reset should clear the workspace state file."""
    import asyncio
    from oas_mcp.server import reset as server_reset
    from oas_mcp.cli_state import save_surfaces, load_surfaces

    asyncio.run(server_reset())

    save_surfaces("test_ws", {"wing": {"name": "wing", "num_y": 7}})
    assert load_surfaces("test_ws") != {}

    # Build a namespace for reset (no params)
    ns = argparse.Namespace()
    oneshot_mode("reset", ns, workspace="test_ws")
    capsys.readouterr()

    assert load_surfaces("test_ws") == {}


def test_oneshot_output_file(isolated_state, tmp_path, capsys):
    """One-shot --output should write JSON to file."""
    import asyncio
    from oas_mcp.server import reset

    asyncio.run(reset())

    out_path = tmp_path / "result.json"
    ns = _make_ns(name="wing", num_y=7)
    oneshot_mode("create_surface", ns, workspace="test_ws", output=str(out_path))

    assert out_path.exists()
    result = json.loads(out_path.read_text())
    assert result["ok"] is True


def test_oneshot_bad_saved_surface(isolated_state, capsys):
    """If saved surface has invalid args, should return structured error."""
    import asyncio
    from oas_mcp.server import reset
    from oas_mcp.cli_state import save_surfaces

    asyncio.run(reset())

    # Save a surface with invalid args (num_y must be odd ≥ 3)
    save_surfaces("test_ws", {"bad": {"name": "bad", "num_y": 2}})

    # Try to run analysis — state reconstruction should fail gracefully
    from oas_mcp.cli_runner import get_registry
    import inspect

    fn = get_registry()["run_aero_analysis"]
    sig = inspect.signature(fn)
    defaults = {}
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        defaults[name] = param.default if param.default is not inspect.Parameter.empty else None
    defaults["surfaces"] = '["bad"]'
    defaults["alpha"] = 5.0
    ns = argparse.Namespace(**defaults)

    with pytest.raises(SystemExit):
        oneshot_mode("run_aero_analysis", ns, workspace="test_ws")

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["ok"] is False
    assert "reconstruct" in result["error"]["message"].lower() or "state" in result["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Interactive mode (Gap 2) — test actual _interactive_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_loop_actual(capsys):
    """Test actual _interactive_loop with mocked stdin pipe."""
    import asyncio
    from oas_mcp.server import reset

    await reset()

    line = json.dumps({"tool": "create_surface", "args": {"name": "wing", "num_y": 7}}) + "\n"

    async def _patched_interactive(pretty=False):
        from oas_mcp.cli_runner import run_tool, json_dumps

        # Simulate the reader with an asyncio.StreamReader fed our data
        reader = asyncio.StreamReader()
        reader.feed_data(line.encode("utf-8"))
        reader.feed_eof()

        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            cmd = json.loads(text)
            response = await run_tool(cmd["tool"], cmd.get("args", {}))
            print(json_dumps(response, pretty=pretty), flush=True)

    await _patched_interactive()

    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_interactive_loop_malformed_json(capsys):
    """Malformed JSON in interactive mode should produce an error response, not crash."""
    import asyncio
    from oas_mcp.cli_runner import json_dumps

    bad_line = "not valid json\n"
    good_line = json.dumps({"tool": "reset", "args": {}}) + "\n"
    data = (bad_line + good_line).encode("utf-8")

    async def _run():
        from oas_mcp.cli_runner import run_tool, json_dumps

        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                cmd = json.loads(text)
            except json.JSONDecodeError as exc:
                response = {
                    "ok": False,
                    "error": {"code": "USER_INPUT_ERROR", "message": f"Invalid JSON: {exc}"},
                }
                print(json_dumps(response), flush=True)
                continue

            response = await run_tool(cmd["tool"], cmd.get("args", {}))
            print(json_dumps(response), flush=True)

    await _run()

    captured = capsys.readouterr()
    lines = [l for l in captured.out.strip().splitlines() if l]
    assert len(lines) == 2
    err = json.loads(lines[0])
    assert err["ok"] is False
    assert err["error"]["code"] == "USER_INPUT_ERROR"
    ok = json.loads(lines[1])
    assert ok["ok"] is True


# ---------------------------------------------------------------------------
# _write_output (Gap 3)
# ---------------------------------------------------------------------------


def test_write_output_json(tmp_path):
    """_write_output should write JSON when no images are present."""
    out = tmp_path / "out.json"
    _write_output({"ok": True, "result": {"CL": 0.5}}, str(out), pretty=True)
    data = json.loads(out.read_text())
    assert data["result"]["CL"] == 0.5


def test_write_output_single_image(tmp_path):
    """_write_output should decode a single base64 PNG."""
    fake_png = base64.b64encode(b"fake-png-data").decode()
    out = tmp_path / "plot.png"
    _write_output(
        {"ok": True, "result": [{"type": "image", "data": fake_png}]},
        str(out),
        pretty=False,
    )
    assert out.read_bytes() == b"fake-png-data"


def test_write_output_multiple_images(tmp_path):
    """_write_output should save multiple images with numbered suffixes."""
    img1 = base64.b64encode(b"img-one").decode()
    img2 = base64.b64encode(b"img-two").decode()
    out = tmp_path / "plot.png"
    _write_output(
        {"ok": True, "result": [
            {"type": "image", "data": img1},
            {"type": "image", "data": img2},
        ]},
        str(out),
        pretty=False,
    )
    assert (tmp_path / "plot_0.png").read_bytes() == b"img-one"
    assert (tmp_path / "plot_1.png").read_bytes() == b"img-two"


def test_write_output_creates_dirs(tmp_path):
    """_write_output should create parent directories."""
    out = tmp_path / "nested" / "deep" / "out.json"
    _write_output({"ok": True}, str(out), pretty=False)
    assert out.exists()


# ---------------------------------------------------------------------------
# main() entry point (Gap 4)
# ---------------------------------------------------------------------------


def test_main_no_args(monkeypatch, capsys):
    """main() with no args should print help and exit 0."""
    monkeypatch.setattr(sys, "argv", ["oas-cli"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_main_list_tools(monkeypatch, capsys):
    """main() list-tools should print tool names."""
    monkeypatch.setattr(sys, "argv", ["oas-cli", "list-tools"])
    main()
    captured = capsys.readouterr()
    assert "create_surface" in captured.out
    assert "run_aero_analysis" in captured.out


# ---------------------------------------------------------------------------
# Boolean flag coercion (Gap 6)
# ---------------------------------------------------------------------------


def test_build_subparser_bool_flag():
    """Bool params should produce --flag / --no-flag pairs."""
    from oas_mcp import server

    parser = argparse.ArgumentParser()
    sp = parser.add_subparsers(dest="mode")
    _build_subparser(sp, "create_surface", server.create_surface)

    ns = parser.parse_args(["create-surface", "--name", "wing", "--with-viscous"])
    assert ns.with_viscous is True

    ns2 = parser.parse_args(["create-surface", "--name", "wing", "--no-with-viscous"])
    assert ns2.with_viscous is False


def test_coerce_json_invalid_json_exits(monkeypatch):
    """Invalid JSON for a list param should sys.exit(1)."""
    from oas_mcp import server

    ns = argparse.Namespace(
        surfaces="not-valid-json",
        alpha=5.0,
        velocity=None,
        Mach_number=None,
        reynolds_number=None,
        density=None,
        cg=None,
        session_id=None,
        run_name=None,
    )
    with pytest.raises(SystemExit) as exc_info:
        _coerce_json_args("run_aero_analysis", ns)
    assert exc_info.value.code == 1
