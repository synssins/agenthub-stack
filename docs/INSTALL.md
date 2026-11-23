# Install guide

Only the room is required. The steps are in order. An agent works after step 2.

## Requirements

| Part | Hardware | Software |
|---|---|---|
| Room (`agenthub/`) | Any Linux, macOS or Windows machine | Python 3.11 or newer |
| A model | A local server or a hosted API | Any server with the OpenAI chat API |
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

Open the address it prints. The steps below use the settings window (gear icon). Data is kept in `agenthub/data/`.

For access from other machines, set `host = "0.0.0.0"` and list the LAN in `allowed_networks`. The room has no login. Run it on a trusted network or behind a VPN.

## 2. A model and an agent

1. Settings > Providers > LLMs: add your model server (its base URL ending in `/v1`, a key if it needs one). Local options: llama.cpp `llama-server`, Ollama, vLLM, LM Studio, or Strata (see `engine/`). Hosted: OpenRouter, Gemini, Anthropic.
2. Settings > Agents > Room agents > Add agent: pick the server and a model, write its role, pick an access level.

House control needs a model that makes tool calls reliably. The tone judge (step 4) and camera descriptions (step 6) can use a second, smaller model. The camera model must read images.

## 3. A voice

```bash
cd speech
docker compose up -d --build        # first start downloads the model (about 4 GB)
```

1. Settings > Voice > Speech providers: add a provider with base URL `http://<host>:4125/v1`.
2. On the agent's page pick the provider and a voice. `Jessica` is included. To add a voice, put a clean recording of 10 to 20 seconds in `speech/refs/` as `<Name>.wav`. The preset is made on first use.
3. Settings > Voice > Speech to text: point at a Wyoming Whisper server.
4. Settings > Voice > Conversation: pick the voice check model, and set a passphrase or PIN if agents may open doors or locks.

Voice devices are added under Settings > Voice > Devices. A device sends speech as WAV files or as a live microphone stream. The protocol is at `/voice-devices.txt` on the running room.

## 4. Feelings and initiative

1. Settings > Agents > Emotion engine: pick the tone model (small and fast) and set the time zone.
2. On the agent's page, "Personality and emotion": switch the engine on. Set the six traits, the resting state and interests. Sleep, the diary, loneliness and boredom each have a switch.
3. "Initiative": switch on to let the agent speak unprompted. Set its channel, the shortest gap between remarks and its quiet hours.

## 5. Home Assistant

1. In Home Assistant create a long-lived access token (user profile > Security).
2. Settings > Senses > Home Assistant: address, token, the agents that hear it and the one that speaks.
3. Add the watched entities: each door, window, lock or leak sensor, whether its opening is said aloud, and reminders.
4. Settings > Senses > Speakers and display: AgentHub's address as Home Assistant reaches it, and a room per Assist satellite. Use "Test a speaker".
5. Settings > Senses > Overview: switch Senses on. The activity list shows what was sensed and what became of it.

For house control give the agent Home Assistant's MCP server: Settings > Providers > MCP servers.

## 6. Cameras (UniFi Protect)

1. On the console: Control Plane > Integrations, create an API key.
2. Settings > Senses > Cameras: console address, key, a model that reads images, agents.
3. Add triggers: one explicit thing on one camera. "Only if it stays" keeps passers-by out. "Said aloud" is No, Always, or "The agent looks and decides".

## 7. Extras

| Feature | Where | Setup |
|---|---|---|
| Reflexes | Settings > Senses > Reflexes | A fixed question asked at once, and one action for yes |
| Desk display | Settings > Providers > MQTT, then Senses > Speakers and display | A face over MQTT. Set the broker first |
| Games | Settings > Agents > Games | Pick the hosts. Download a trivia source or add questions |
| Voice recognition | Settings > Voice > Voice recognition | `pip install sherpa-onnx numpy`, download a speaker-embedding ONNX model, set its path, then enroll each person with "Enroll a voice" |
| Discord | Settings > Keys and access > Discord bots | A bot token from the Discord Developer Portal, the agent it speaks for, the servers it may join |

## Running it as a service (Linux)

```ini
# /etc/systemd/system/agenthub.service
[Unit]
Description=AgentHub
After=network-online.target

[Service]
User=<user>
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

HTTP API: [API.md](API.md). Security notes: [SECURITY.md](SECURITY.md).
