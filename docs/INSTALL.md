# Install guide

Each part is optional except the room. Set them up in this order; after step 2 you have a working agent.

## What you need

| Part | Hardware | Software |
|---|---|---|
| Room (`agenthub/`) | Any Linux, macOS or Windows machine | Python 3.11 or newer |
| A model | Your choice: a local server or a hosted API | Anything that speaks the OpenAI chat API |
| Speech (`speech/`) | An NVIDIA GPU with about 6 GB free | Docker with the NVIDIA container runtime |
| Speech to text | CPU is enough | A Wyoming Whisper server |
| Senses | none | Home Assistant, UniFi Protect, an MQTT broker: each optional |

## 1. The room

```bash
cd agenthub
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install --no-deps "pynacl>=1.6.2"               # over the cap of discord.py[voice]: 1.5.0 has known vulnerabilities
cp config.local.toml.example config.local.toml     # set host, port and allowed_networks
python run.py
```

Open the address it prints. Everything below is done in the settings window (gear icon). The room keeps its data in `agenthub/data/`; nothing there is part of this repository.

To reach it from other machines set `host = "0.0.0.0"` and list your LAN in `allowed_networks`. It has no login of its own: keep it on a network you trust, or behind a VPN.

## 2. A model and an agent

1. Settings > Providers > LLMs: add your model server (its base URL ending in `/v1`, a key if it needs one). Local options: llama.cpp `llama-server`, Ollama, vLLM, LM Studio, or Strata (see `engine/`). Hosted: OpenRouter, Gemini, Anthropic.
2. Settings > Agents > Room agents > Add agent: pick the server and a model, write its role, pick an access level.

A model that follows tool calls well is needed for house control. A second, small and fast model is worth having for the tone judge (step 4) and for describing cameras (step 6, it must read images).

## 3. A voice

```bash
cd speech
docker compose up -d --build        # first start downloads the model (about 4 GB)
```

1. Settings > Voice > Speech providers: add a provider with base URL `http://<host>:4125/v1`.
2. On the agent's page pick the provider and a voice. One example voice ships (`Jessica`). Add your own: put a clean 10 to 20 second recording `<Name>.wav` in `speech/refs/`; a preset is made on first use. Only use recordings you have the right to use.
3. Settings > Voice > Speech to text: point at a Wyoming Whisper server.
4. Settings > Voice > Conversation: pick the voice check model, and set a passphrase or PIN if agents may open doors or locks.

Voice devices (ESPHome satellites) are adopted under Settings > Voice > Devices.

## 4. Feelings and initiative

1. Settings > Agents > Emotion engine: pick the tone model (small and fast) and set your time zone.
2. On the agent's page, "Personality and emotion": switch the engine on and set the six traits, the resting state, interests, and what you want of sleep, the diary, loneliness and boredom.
3. "Initiative": switch on if the agent may speak unprompted; set its channel, the least gap and its quiet hours.

## 5. Home Assistant

1. In Home Assistant create a long-lived access token (your profile > Security).
2. Settings > Senses > Home Assistant: address, token, the agents that hear it and the one that speaks.
3. Add the watched things: each door, window, lock or leak sensor, whether its opening is said aloud, and reminders.
4. Settings > Senses > Speakers and display: AgentHub's address as Home Assistant reaches it, and a room per Assist satellite. Use "Test a speaker".
5. Settings > Senses > Overview: switch Senses on. The activity list shows what was sensed and what became of it.

For house control give the agent Home Assistant's MCP server: Settings > Providers > MCP servers.

## 6. Cameras (UniFi Protect)

1. On the console: Control Plane > Integrations, create an API key.
2. Settings > Senses > Cameras: console address, key, a model that reads images, agents.
3. Add triggers: one explicit thing on one camera. "Only if it stays" keeps passers-by out. "Said aloud" is No, Always, or "The agent looks and decides".

## 7. Extras

- **Reflexes** (Settings > Senses > Reflexes): a fixed question asked at once, and one action for yes.
- **Desk display**: a face over MQTT; set the broker in Settings > Providers > MQTT, then Senses > Speakers and display.
- **Games** (Settings > Agents > Games): pick the hosts; download a trivia source or add your own questions.
- **Who is speaking** (Settings > Voice): `pip install sherpa-onnx numpy`, download a speaker-embedding ONNX model, set its path, enrol each person.
- **Discord** (Settings > Keys and access > Discord bots): a bot token from the Discord Developer Portal, the agent it speaks for, and the servers it may join.

## Running it as a service (Linux)

```ini
# /etc/systemd/system/agenthub.service
[Unit]
Description=AgentHub
After=network-online.target

[Service]
User=<your user>
WorkingDirectory=/path/to/agenthub
ExecStart=/path/to/agenthub/.venv/bin/python run.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Checks

```bash
cd agenthub && python -m pytest -q tests          # the room's own tests
cd speech && python -m pytest -q                  # inside the speech container
```

The HTTP API is listed in [API.md](API.md); security notes are in [SECURITY.md](SECURITY.md).
