"""Entry point — starts MCP server (port 8200) + web UI (port 8300)."""

import argparse
import asyncio
import secrets
import sys
import threading
import time
import logging
from pathlib import Path

# Ensure the project directory is on the import path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Start agentchattr (web UI + MCP server).",
        epilog="Flags override config.toml for this invocation. The same flags "
               "are also accepted by wrapper.py and wrapper_api.py so a launcher "
               "can isolate per-project instances by passing matching values to "
               "each process.",
    )
    parser.add_argument("--data-dir",      default=None, help="Override server.data_dir (path)")
    parser.add_argument("--port",          default=None, help="Override server.port (int)")
    parser.add_argument("--mcp-http-port", default=None, help="Override mcp.http_port (int)")
    parser.add_argument("--mcp-sse-port",  default=None, help="Override mcp.sse_port (int)")
    parser.add_argument("--upload-dir",    default=None, help="Override images.upload_dir (path)")
    parser.add_argument("--allow-network", action="store_true",
                        help="Allow binding to non-localhost hosts (with confirmation).")
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse flags for --help support; the actual env propagation happens via
    # the shared config_loader.apply_cli_overrides helper so run.py and the
    # wrappers use identical extraction logic.
    _parse_args()

    from config_loader import apply_cli_overrides, load_config
    apply_cli_overrides()

    config_path = ROOT / "config.toml"
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    config = load_config(ROOT)

    # --- Security: generate a random session token (in-memory only) ---
    session_token = secrets.token_hex(32)

    # Configure the FastAPI app (creates shared store)
    from app import app, configure, set_event_loop, store as _store_ref
    configure(config, session_token=session_token)

    # Rulebooks (style/process guides), mirrored to ./rulebooks/*.md for CLI agents
    import rulebooks
    rulebooks.setup(config, ROOT)
    import voice
    voice.setup(config)

    # Settings back end: /v1 keys, self-registration, /llms.txt, IRC
    import admin_api
    admin_api.setup(sys.modules["app"], config)
    app.include_router(admin_api.router)

    # Shared code artifacts (long fenced blocks in messages, versioned)
    import artifacts
    artifacts.setup(sys.modules["app"], config)
    app.include_router(artifacts.router)

    # OpenAI-compatible /v1 surface (room + agents as models)
    import v1_api
    v1_api.setup(sys.modules["app"], config)
    app.include_router(v1_api.router)

    # Share stores with the MCP bridge
    from app import store, rules, summaries, jobs, room_settings, registry, router as app_router, agents as app_agents, session_engine, session_store
    import mcp_bridge
    mcp_bridge.store = store
    mcp_bridge.rules = rules
    mcp_bridge.summaries = summaries
    mcp_bridge.jobs = jobs
    mcp_bridge.room_settings = room_settings
    mcp_bridge.registry = registry
    mcp_bridge.config = config
    mcp_bridge.router = app_router
    mcp_bridge.agents = app_agents

    # Enable cursor and role persistence across restarts
    data_dir = ROOT / config.get("server", {}).get("data_dir", "./data")
    mcp_bridge._CURSORS_FILE = data_dir / "mcp_cursors.json"
    mcp_bridge._load_cursors()
    mcp_bridge._ROLES_FILE = data_dir / "roles.json"
    mcp_bridge._load_roles()

    # Start MCP servers in background threads
    http_port = config.get("mcp", {}).get("http_port", 8200)
    sse_port = config.get("mcp", {}).get("sse_port", 8201)
    mcp_bridge.mcp_http.settings.port = http_port
    mcp_bridge.mcp_sse.settings.port = sse_port

    threading.Thread(target=mcp_bridge.run_http_server, daemon=True).start()
    threading.Thread(target=mcp_bridge.run_sse_server, daemon=True).start()
    time.sleep(0.5)
    logging.getLogger(__name__).info("MCP streamable-http on port %d, SSE on port %d", http_port, sse_port)

    # Mount static files
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse

    static_dir = ROOT / "static"

    @app.get("/")
    async def index():
        # Read index.html fresh each request so changes take effect without restart.
        # Inject the session token into the HTML so the browser client can use it.
        # This is safe: same-origin policy prevents cross-origin pages from reading
        # the response body, so only the user's own browser tab gets the token.
        html = (static_dir / "index.html").read_text("utf-8")
        try:  # emotion tags of the speech engines: spoken, never shown (letters and spaces only, voice.TAG_RE)
            import json
            import voice
            said = json.dumps(await asyncio.to_thread(voice.all_tags))
        except Exception:
            said = "[]"
        injected = html.replace(
            "</head>",
            f'<script>window.__SESSION_TOKEN__="{session_token}";window.__VOICE_TAGS__={said};</script>\n</head>',
        )
        return HTMLResponse(injected, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Capture the event loop for the store→WebSocket bridge
    @app.on_event("startup")
    async def on_startup():
        set_event_loop(asyncio.get_running_loop())
        # Resume any sessions that were active before restart
        if session_engine:
            session_engine.resume_active_sessions()
        # IRC gateway ([irc] in config): room as #channels, agents as nicks
        import app as hub
        import irc_gateway
        import json as _json
        irc_override = Path(config.get("server", {}).get("data_dir", "./data")) / "irc.json"
        if irc_override.exists():  # password set from the settings UI wins over config
            config.setdefault("irc", {}).update(_json.loads(irc_override.read_text("utf-8")))
        started = await irc_gateway.start(hub, config)
        hub.irc_gateway = started[0] if started else None
        # Room agents created in the web UI run in-process
        import agent_runner
        hub.agent_manager = agent_runner.AgentManager(hub, config.get("server", {}).get("data_dir", "./data"))
        hub.agent_manager.start_all()
        # Feelings for the agents that have the emotion engine switched on
        import emotion_engine
        emotion_engine.start(hub)
        # ...and the agents that may start talking by themselves
        import initiative
        initiative.start(hub)
        # ...and what they know of the house: Home Assistant, cameras, speakers (off until set up)
        import senses
        senses.start(hub)
        import games
        games.data_dir = Path(hub.agent_manager.data_dir)
        import trivia
        trivia.data_dir = Path(hub.agent_manager.data_dir)
        import speakers
        speakers.data_dir = Path(hub.agent_manager.data_dir)
        # Discord bots (Settings > Keys and access > Discord bots)
        try:
            import discord_gateway
            hub.discord = await discord_gateway.start(hub)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Discord gateway not started")

    # Run web server
    import uvicorn
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8300)

    # --- Security: warn if binding to a non-localhost address ---
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\n  !! SECURITY WARNING — binding to {host} !!")
        print("  This exposes agentchattr to your local network.")
        print()
        print("  Risks:")
        print("  - No TLS: traffic (including session token) is plaintext")
        print("  - Anyone on your network can sniff the token and gain full access")
        print("  - With the token, anyone can @mention agents and trigger tool execution")
        print("  - If agents run with auto-approve, this means remote code execution")
        print()
        print("  Only use this on a trusted home network. Never on public/shared WiFi.")
        nets = config.get("server", {}).get("allowed_networks", [])
        print(f"  Allowed client networks: {', '.join(nets) or 'loopback only'}")
        if config.get("server", {}).get("allow_network"):
            # Accepted in config (headless service): no interactive prompt.
            print("  [server] allow_network = true: starting.\n")
        elif "--allow-network" not in sys.argv:
            print("  Pass --allow-network to start anyway, or set host to 127.0.0.1.\n")
            sys.exit(1)
        else:
            print()
            try:
                confirm = input("  Type YES to accept these risks and start: ").strip()
            except (EOFError, KeyboardInterrupt):
                confirm = ""
            if confirm != "YES":
                print("  Aborted.\n")
                sys.exit(1)

    print(f"\n  agentchattr")
    print(f"  Web UI:  http://{host}:{port}")
    print(f"  MCP HTTP: http://{host}:{http_port}/mcp  (Claude, Codex)")
    print(f"  MCP SSE:  http://{host}:{sse_port}/sse   (Gemini)")
    print(f"  Data:    {data_dir}")
    print(f"  Agents auto-trigger on @mention")
    print(f"\n  Session token: {session_token}\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

