"""Автотест T7.5-4/5 (QA 7.5 CI-PROVIDER-BY-TRANSPORT/INV-TOKENSTORE-BY-FLAG): entry-point garmin-tn-mcp + профильная установка.

Приёмка (1) — автоматизируемая, здесь:
  A. install_tn пишет корректный per-slug конфиг (env, merge-safe, идемпотентно,
     сырьевой garmin-raw цел) + предупреждение при профиле без токенов и без --tokenstore.
  B. entry-резолв через CI-PROVIDER-BY-TRANSPORT-отказ: garmin-tn-mcp БЕЗ env → быстрый ненулевой exit с нашим
     текстом про GARMIN_TN_PROFILE. Не резолвится (нет в PATH) → SKIP с инструкцией.
     Резолвится, но не наш текст → КРАСНЫЙ (entry указывает не туда). Таймаут → КРАСНЫЙ
     (CI-PROVIDER-BY-TRANSPORT-падение должно быть ДО mcp.run(), без stdio-лупа).
  C. MCP handshake: initialize + tools/list → 8 тулов garmin_*, slug НЕ в inputSchema.
     Через module-invocation (всегда доступно), чтобы проверять поверхность даже без
     установленного entry; таймаут+stderr против немого висяка.
Приёмка (2) — «Claude Desktop видит два коннектора» — ручная, в бою; НЕ здесь (нельзя
закоммитить: нужен Claude + реальный конфиг).

Самодостаточен: temp home/конфиг, синтетика, путь к модулям от __file__.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "garmin_raw"))

_EXPECTED_TOOLS = {
    "garmin_status", "garmin_query", "garmin_compact", "garmin_full",
    "garmin_aggregates", "garmin_add_lactate", "garmin_add_note", "garmin_delete_mark",
}


# ── A. install-конфиг (in-process) ────────────────────────────────────────────
def test_install_config():
    import install as inst
    tmp = tempfile.mkdtemp()
    cfg = Path(tmp) / "claude_desktop_config.json"
    # предзаполняем: сырьевой коннектор + пользовательская настройка (проверка merge-safe)
    cfg.write_text(json.dumps({
        "mcpServers": {"garmin-raw": {"command": "uv", "args": ["x"]}},
        "userPref": "keep-me",
    }))
    inst._config_path = lambda: cfg          # редирект на temp
    inst._uv_bin = lambda: "/fake/uv"        # uv в песочнице может отсутствовать
    import profiles
    profiles.ROOT = Path(tmp)                # чтобы warn-проверка смотрела в temp

    # установка с --tokenstore → env содержит оба ключа, сырьевой и userPref целы
    sys.argv = ["garmin-tn-install", "anton", str(_ROOT), "--tokenstore", "/tok/store"]
    out = io.StringIO()
    with redirect_stdout(out):
        inst.install_tn()
    data = json.loads(cfg.read_text())
    srv = data["mcpServers"]["garmin-tn-anton"]
    assert srv["env"] == {"GARMIN_TN_PROFILE": "anton", "GARMIN_TOKENSTORE": "/tok/store"}
    assert srv["args"] == ["--directory", str(_ROOT), "run", "garmin-tn-mcp"]
    assert "garmin-raw" in data["mcpServers"], "сырьевой коннектор затронут!"
    assert data["userPref"] == "keep-me", "merge не сохранил чужие ключи!"
    assert "⚠" not in out.getvalue(), "с --tokenstore предупреждения быть не должно"

    # идемпотентность: повторная установка не плодит, ключ один
    inst.install_tn()
    data = json.loads(cfg.read_text())
    assert list(data["mcpServers"]).count("garmin-tn-anton") == 1

    # второй профиль — оба сосуществуют, сырьевой цел
    sys.argv = ["garmin-tn-install", "mila", str(_ROOT), "--tokenstore", "/tok/mila"]
    with redirect_stdout(io.StringIO()):
        inst.install_tn()
    data = json.loads(cfg.read_text())
    assert {"garmin-raw", "garmin-tn-anton", "garmin-tn-mila"} <= set(data["mcpServers"])

    # предупреждение: профиль без своих токенов И без --tokenstore
    sys.argv = ["garmin-tn-install", "bob", str(_ROOT)]
    out = io.StringIO()
    with redirect_stdout(out):
        inst.install_tn()
    assert "⚠" in out.getvalue() and "--tokenstore" in out.getvalue(), \
        "нет предупреждения о профиле без токенов"

    # uninstall убирает только свой ключ, сырьевой и другие профили целы
    sys.argv = ["garmin-tn-uninstall", "anton"]
    with redirect_stdout(io.StringIO()):
        inst.uninstall_tn()
    data = json.loads(cfg.read_text())
    assert "garmin-tn-anton" not in data["mcpServers"]
    assert {"garmin-raw", "garmin-tn-mila"} <= set(data["mcpServers"])
    print("A install-конфиг: per-slug env, merge-safe, идемпотент, warn, uninstall ✓")


# ── B. entry-резолв через CI-PROVIDER-BY-TRANSPORT-отказ ────────────────────────────────────────────
def test_entry_resolves_via_q7():
    env = {k: v for k, v in os.environ.items() if k != "GARMIN_TN_PROFILE"}
    try:
        p = subprocess.run(["garmin-tn-mcp"], capture_output=True, text=True,
                           timeout=15, env=env)
    except FileNotFoundError:
        print("B entry-резолв: SKIP — 'garmin-tn-mcp' не в PATH "
              "(запусти через `uv run` или `uv sync` для установки entry-points)")
        return
    except subprocess.TimeoutExpired:
        raise AssertionError("garmin-tn-mcp БЕЗ профиля завис — CI-PROVIDER-BY-TRANSPORT-падение должно быть "
                             "ДО mcp.run() (регресс порядка в main()?)")
    assert p.returncode != 0, f"без профиля должен упасть (CI-PROVIDER-BY-TRANSPORT); rc={p.returncode}"
    assert "GARMIN_TN_PROFILE" in p.stderr, (
        f"резолвится, но stderr не наш CI-PROVIDER-BY-TRANSPORT-текст — entry указывает не туда?\n"
        f"rc={p.returncode} stderr={p.stderr!r}")
    print("B entry-резолв через CI-PROVIDER-BY-TRANSPORT-отказ ✓")


# ── C. MCP handshake: initialize + tools/list ─────────────────────────────────
async def _list_tools(env, cwd):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "garmin_raw.server_enriched"],
        env=env, cwd=str(cwd))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


def test_mcp_handshake():
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "GARMIN_TN_PROFILE": "testp", "GARMIN_TN_HOME": tmp,
           "PYTHONPATH": str(_ROOT)}
    try:
        tools = asyncio.run(asyncio.wait_for(_list_tools(env, _ROOT), timeout=25))
    except asyncio.TimeoutError:
        raise AssertionError("MCP handshake завис (>25с): сервер не поднялся или не "
                             "ответил на tools/list")
    names = {t.name for t in tools}
    assert names == _EXPECTED_TOOLS, f"набор тулов не тот: {names ^ _EXPECTED_TOOLS}"
    for t in tools:                                    # I2.1: slug НЕ в схеме
        props = (t.inputSchema or {}).get("properties", {})
        assert "slug" not in props, f"slug в inputSchema тула {t.name}"
    print(f"C MCP handshake: {len(names)} тулов garmin_*, slug не в схемах ✓")


if __name__ == "__main__":
    test_install_config()
    test_entry_resolves_via_q7()
    test_mcp_handshake()
    print("T7.5-4/5 ENTRY + INSTALL TEST PASSED")
