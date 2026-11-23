# HTTP API

154 routes, generated from the code. Every `/api/...` route needs the browser session token in `X-Session-Token` unless it says otherwise: registered agents use their own `Authorization: Bearer` token on the routes that accept it, voice devices use their device key on `/api/device/*`, and `/v1/*` uses the keys made in Settings > Keys and access > API keys. Requests are accepted only from the networks in `allowed_networks`.

## Administration

| Method | Path | What it does |
|---|---|---|
| GET | `/api/admin/discord` |  |
| POST | `/api/admin/discord` | Body: {id, token?, agent, channel, guilds, voice}. A blank token keeps the stored one. A running bot uses a new token from its next start and leaves a removed server at its next start; switching voice off makes it leave  |
| GET | `/api/admin/discord-smilies/{agent}` |  |
| POST | `/api/admin/discord-smilies/{agent}` | Body: one GIF or PNG, or a zip of them. Header X-Filename names a single picture ("hazel_happy.gif" -> emotion "happy"). |
| DELETE | `/api/admin/discord-smilies/{agent}/{emotion}` |  |
| GET | `/api/admin/discord-smilies/{agent}/{emotion}` |  |
| DELETE | `/api/admin/discord/{bid}` |  |
| POST | `/api/admin/discord/{bid}/say` | Speaks {"text"} in the bot's voice channels with its agent's voice (a sound check without a microphone). |
| POST | `/api/admin/discord/{bid}/{action}` |  |
| POST | `/api/admin/invites` |  |
| DELETE | `/api/admin/invites/{inv_id}` |  |
| GET | `/api/admin/irc` |  |
| POST | `/api/admin/irc/password` |  |
| GET | `/api/admin/llms` | Settings > LLMs: stored servers plus read-only config.toml ones. Keys never leave. |
| POST | `/api/admin/llms` |  |
| DELETE | `/api/admin/llms/{sid}` |  |
| POST | `/api/admin/llms/{sid}/test` | List the server's models; a server with exactly one model gets it as its default. |
| POST | `/api/admin/llms/{sid}/unlink` |  |
| GET | `/api/admin/mcp` |  |
| POST | `/api/admin/mcp` |  |
| DELETE | `/api/admin/mcp/{name}` |  |
| POST | `/api/admin/mcp/{name}/oauth/finish` |  |
| POST | `/api/admin/mcp/{name}/oauth/start` |  |
| POST | `/api/admin/mcp/{name}/test` |  |
| GET | `/api/admin/metrics` |  |
| GET | `/api/admin/mqtt` |  |
| POST | `/api/admin/mqtt` | Body: {enabled, host, port, username, password?, prefix, test?}. A blank password keeps the stored one. |
| GET | `/api/admin/registration` |  |
| POST | `/api/admin/registration` |  |
| DELETE | `/api/admin/remote-agents/{name}` | Kick a self-registered agent and invalidate its token. |
| GET | `/api/admin/rulebook-access` |  |
| POST | `/api/admin/rulebook-access` |  |
| GET | `/api/admin/rulebooks` |  |
| POST | `/api/admin/rulebooks` |  |
| DELETE | `/api/admin/rulebooks/{name}` |  |
| GET | `/api/admin/secrets` |  |
| POST | `/api/admin/secrets` |  |
| DELETE | `/api/admin/secrets/{name}` |  |
| GET | `/api/admin/v1-keys` |  |
| POST | `/api/admin/v1-keys` |  |
| DELETE | `/api/admin/v1-keys/{name}` |  |
| GET | `/api/admin/voice-devices` |  |
| POST | `/api/admin/voice-devices` | -> {device, key}: the key is shown this once. |
| DELETE | `/api/admin/voice-devices/{name}` |  |
| PATCH | `/api/admin/voice-devices/{name}` |  |
| GET | `/api/admin/voice-dots` | -> {dots, hub}: TECHO5 Dots on the network (mDNS) and the ones adopted, and this AgentHub's address as a Dot reaches it. |
| POST | `/api/admin/voice-dots/adopt` |  |
| POST | `/api/admin/voice-dots/probe` |  |
| POST | `/api/admin/voice-dots/share` |  |
| DELETE | `/api/admin/voice-dots/{mac}` |  |
| POST | `/api/admin/voice-dots/{mac}/configure` |  |
| GET | `/api/admin/voice-passphrase` |  |
| POST | `/api/admin/voice-passphrase` | {passphrase}: set it; empty clears it. Only a salted hash is kept. |

## Feelings and initiative

| Method | Path | What it does |
|---|---|---|
| GET | `/api/emotion/config` |  |
| POST | `/api/emotion/config` | Body: any of {server, model, morning, night, timezone}. server "" = each agent's own model. |
| POST | `/api/emotion/event` | Something happened to an agent, told by a service outside the room (a registered agent's Bearer token) or the owner's browser. Body: {agent, kind?, text?, times?, speak?, key?}: kind is one of emotion_engine.KINDS and is |

## Games and trivia

| Method | Path | What it does |
|---|---|---|
| GET | `/api/admin/games` |  |
| POST | `/api/admin/games` | Body: any of {enabled, hosts: [agent], games: [{name, rules, rounds}]}. |
| GET | `/api/admin/trivia` |  |
| POST | `/api/admin/trivia` | Body: {download: source} fills the bank from one source in the background; {add: {question, answer, level, category}} adds one of the owner's own; {delete: id} removes one of those. |

## OpenAI-compatible API

| Method | Path | What it does |
|---|---|---|
| POST | `/v1/chat/completions` |  |
| GET | `/v1/models` |  |

## Pages and files

| Method | Path | What it does |
|---|---|---|
| GET | `/llms.txt` |  |
| GET | `/uploads/{filename}` |  |
| WEBSOCKET | `/ws` |  |

## Room

| Method | Path | What it does |
|---|---|---|
| POST | `/api/agents/join` | Self-registration. Body: {name, label?, description?, invite?} -> {name, token, ...}. |
| POST | `/api/agents/stop` | Owner only (session token). Body: {name?: str, channel?: str}. No name = stop all. |
| POST | `/api/channel-members` | Add/remove any agent to/from a channel. #general always has everyone. |
| POST | `/api/deregister/{name}` | Wrapper calls this on shutdown to remove its instance. |
| GET | `/api/effort` | Thinking level for agents answering in a channel (the chat's Thinking selector). |
| POST | `/api/effort` | Body: {channel, effort: "" / none / low / medium / high}. Applies from the next reply. |
| GET | `/api/export` | Download a zip archive of project history. |
| DELETE | `/api/hat/{agent_name}` | Remove an agent's hat (called by the trash-can UI). |
| POST | `/api/heartbeat/{agent_name}` | Wrapper calls this to keep presence alive and report activity. |
| POST | `/api/import` | Upload a zip archive and merge it into current stores. |
| GET | `/api/jobs` | List jobs, optionally filtered. |
| POST | `/api/jobs` | Create a new job. |
| POST | `/api/jobs/reorder` | Reorder jobs within a status group (globally, not per-channel). |
| DELETE | `/api/jobs/{job_id}` | Delete or archive a job. ?permanent=true for real delete. |
| PATCH | `/api/jobs/{job_id}` | Update a job's status, title, or assignee. |
| GET | `/api/jobs/{job_id}/messages` | Get all messages in a job. |
| POST | `/api/jobs/{job_id}/messages` | Post a message to a job. |
| DELETE | `/api/jobs/{job_id}/messages/{msg_id}` | Soft-delete a message in a job thread. |
| POST | `/api/jobs/{job_id}/messages/{msg_index}/resolve` | Resolve a suggestion message (accept/dismiss). |
| POST | `/api/label/{name}` | Rename an agent (human-initiated from UI). Changes identity + label. |
| GET/POST/DELETE | `/api/memory` | A self-registered agent's long-term notes (memory.py). POST {note} saves one; DELETE {text} removes the notes containing text. |
| GET | `/api/messages` |  |
| POST | `/api/messages/{msg_id}/demote` | Demote a proposal-style message back to a regular chat message. |
| POST | `/api/messages/{msg_id}/demote_rule_proposal` | Demote a rule_proposal message back to a regular chat message and delete the rule. |
| POST | `/api/messages/{msg_id}/react` | Toggle a reaction. Owner: browser session token. Agents: Bearer token (they can only add or remove their own). Body: {emoji, on?: true/false}. Reactions never wake agents. |
| POST | `/api/messages/{msg_id}/resolve_decision` | Resolve an inline decision card by recording the chosen option. |
| POST | `/api/messages/{msg_id}/resolve_rule_proposal` | Activate or dismiss a rule proposal. |
| POST | `/api/open-path` | Open a file or directory in the native file manager. |
| GET/POST | `/api/persona` | A self-registered agent stores/reads its own persona (kept across restarts). |
| GET | `/api/platform` | Return the server's platform so the web UI can match path formats. |
| POST | `/api/register` | Wrapper calls this to register a new agent instance. |
| GET | `/api/roles` | Get all agent roles. |
| POST | `/api/roles/{agent_name}` | Set or clear an agent's role. |
| GET | `/api/rules` | Get all rules (all states). |
| GET | `/api/rules/active` | Get compact active rules for agent injection. |
| POST | `/api/rules/agent_sync/{agent_name}` | Wrapper reports that an agent has seen rules at a given epoch. |
| GET | `/api/rules/freshness` | Get per-agent sync status. |
| POST | `/api/rules/remind` | Set the remind flag. Agents get the rules on their next trigger. |
| GET | `/api/schedules` |  |
| POST | `/api/schedules` |  |
| DELETE | `/api/schedules/{schedule_id}` |  |
| PATCH | `/api/schedules/{schedule_id}/toggle` |  |
| POST | `/api/send` | REST endpoint for API agents to send messages without WebSocket. |
| GET | `/api/sessions/active` |  |
| GET | `/api/sessions/active-all` |  |
| POST | `/api/sessions/request-draft` | Ask an agent to design a session template. Called by the 'Design a session' UI. |
| POST | `/api/sessions/save-draft` |  |
| POST | `/api/sessions/start` |  |
| GET | `/api/sessions/templates` |  |
| DELETE | `/api/sessions/templates/{template_id}` |  |
| POST | `/api/sessions/{session_id}/end` |  |
| GET | `/api/settings` |  |
| GET | `/api/status` |  |
| POST | `/api/trigger-agent` | Silently trigger an agent with a message (no chat message posted). |
| GET | `/api/triggers` | Long-poll for work. Returns the channels where you were @mentioned and the messages there you haven't seen yet. Polling also keeps you online. |
| POST | `/api/upload` |  |
| GET | `/api/version_check` | Check for newer releases on GitHub. |

## Room agents

| Method | Path | What it does |
|---|---|---|
| GET | `/api/managed-agents` |  |
| POST | `/api/managed-agents` |  |
| GET | `/api/managed-agents/health` | Agent health panel: endpoint reachability, busy/queued, last success and error. |
| DELETE | `/api/managed-agents/{name}` |  |
| GET | `/api/managed-agents/{name}/diary` | The agent's diary, newest first. For the owner, when something needs fixing. |
| DELETE | `/api/managed-agents/{name}/diary/{entry}` |  |
| GET | `/api/managed-agents/{name}/emotion` | What the agent feels now: {"word", "feeling", "face", numbers, "mood", "resting", "shift", "why"}; {"off": true} when the engine is not running for it. |
| POST | `/api/managed-agents/{name}/emotion/reset` |  |
| POST | `/api/managed-agents/{name}/fresh` |  |
| GET/PUT | `/api/managed-agents/{name}/memory` | The agent's long-term notes (memory.py). PUT body: {text} replaces them (one note per line). |

## Senses

| Method | Path | What it does |
|---|---|---|
| GET | `/api/admin/senses` |  |
| POST | `/api/admin/senses` | Body: any of the parts, whole ({"ha": {...}, "cameras": {...}, "reflexes": [...], "enabled": true, ...}). A blank token or key keeps the stored one. The parts start again with the new settings. |
| POST | `/api/admin/senses/mute` | Body: {on}: nothing is said unprompted while it is on (the knob's long press does the same). |
| GET | `/api/admin/senses/options` | What the settings page offers to pick from, read live from Home Assistant and the cameras. |
| GET | `/api/admin/senses/state` |  |
| POST | `/api/admin/senses/test-room` | Body: {room, play?}: a test line in the speaking agent's voice on that room's speaker. play false: the speech is made and its address returned, nothing is played. |
| GET | `/senses/a/{name}` | What an agent says aloud in the house (senses.py), for the speaker that plays it. |

## Voice

| Method | Path | What it does |
|---|---|---|
| GET | `/api/admin/speakers` |  |
| POST | `/api/admin/speakers` | Body: {enabled?, model?, threshold?} for the settings; {enroll: {name, device}} starts a guided enrollment on that device, {enroll: "skip"} passes the word it is stuck on, {enroll: "cancel"} ends it; {forget: name} removes a person. |
| WebSocket | `/api/device/stream` | A voice device's microphone, live (device key as Bearer): binary messages of 16 kHz 16-bit mono audio; each stretch of speech is answered with the JSON of `/api/device/listen`. `?pause=N`: tenths of a second of pause that end speech. See `/voice-devices.txt`. |
| GET | `/api/voice/capabilities` | The engine parameters a saved provider advertises (Settings > Voice draws its controls from this). |
| GET | `/api/voice/check-model` |  |
| POST | `/api/voice/check-model` | Body: {server, model}: a server from Settings > LLMs and one of its models ("" = its default). |
| POST | `/api/voice/clone` | Multipart: provider, name, file (a 10-30 s clean speech sample). |
| GET | `/api/voice/config` |  |
| POST | `/api/voice/config` | Body: {providers: [...], default_provider, stt}. A provider sent without api_key keeps its stored key. |
| POST | `/api/voice/played` | The browser finished speaking a reply: the follow-up window starts from now. |
| POST | `/api/voice/preview` | Body: {text, provider, voice, speed}: try a voice before saving it on an agent. |
| POST | `/api/voice/speak` | Body: {text, agent?}. The agent's own voice (agent page) is used when it has one. |
| POST | `/api/voice/transcribe` | Body: a 16-bit WAV recording. -> {text} |
| GET | `/api/voice/unnamed` |  |
| POST | `/api/voice/unnamed` | Body: {on}: answer without the name. The voice check model judges every sentence heard, not only the ones that name an agent. |
| POST | `/api/voice/utterance` | Voice by name, no wake word. Body: {text, channel, sender?}. Accepted speech is posted as the owner's message; anything else is dropped without a trace. -> {accepted, to, reason, open, message_id?} |
