# AgentHub roadmap (2026-11-11)

Owner decisions in order of delivery. Each phase ships live when its tests pass.

## 1. UI foundation
- Theme system: Charcoal (default), Graphite, Midnight (OLED), Light. Accent is fixed per theme. Selected in Settings > Appearance, stored per browser.
- Settings window regrouped into clear categories (Appearance, Notifications, Chat, Agents, Rulebooks, Integrations, Data).
- Rulebooks list: descriptions wrap; no horizontal overflow.
- Per-agent settings (notification sound, later voice) live on the agent's own configuration page, not in General.

## 2. Shared artifacts
- Code blocks of 8+ lines become artifacts stored on the server with version history.
- Message shows an artifact card; clicking opens a right-side panel over the chat. Copy icon pinned top right, download, click outside or Esc to close.
- Agents in the channel can read and update artifacts (new version, announced in the channel).

## 3. Control and conversation
- `/stop` stops every working agent; `/stop @name` stops one. Stop buttons in the UI.
- Reactions: multiple emoji per message, including sour, angry and silly. Agents can react in their persona's style.
- Threads: reply to a message without cluttering the channel.

## 4. Images
- ComfyUI image generation through the Nexus comfy_mcp server, registered in Settings > MCP servers and granted per agent.
- Agent-posted images show as thumbnails; click for a lightbox (full size, previous/next, download, prompt details).
- Workflow catalog: each workflow tagged by content (NSFW, NSFW-capable, SFW-leaning), model and best use, so agents pick the right one.
- Masks: paint a mask in the lightbox, or have an agent make one from a description (Florence-2 + SAM2) for inpainting.
- Per-server MCP tool timeout for long image runs.

## 4b. Agent memory and fixes
- Rulebooks on request: agents open one with the rulebook tool only when asked; fine-grained access is an advanced option.
- Context compaction for API agents: messages that leave the context window are folded into a rolling summary per agent and channel; long tool loops compact older tool results; `/compact @agent` forces it.
- Painted masks keep the original pixels under the mask (they turn black today, which hurts partial-denoise inpaints).
- A stopped API agent drops the cancelled request from its context.
- Video and audio outputs play inline in chat.
- Thread composer gets attachments and @-autocomplete.
- IRC shows artifacts as a title and link, not a bare token.
- Timed-out ComfyUI jobs are cancelled instead of left running.
- Agent health panel: model, endpoint up/down, last error and queue per agent.

## 5. Voice output
- Pluggable TTS providers (OpenAI-style /v1/audio/speech: Chatterbox :4123 with cloning, Kokoro :8880, external APIs).
- Voice library with cloning from a short sample. Voice and speed are set on each agent's configuration page.
- Play button per message, auto-speak toggle per channel, streamed playback; code and artifacts are not read aloud.

## 6. Voice conversation
- Push-to-talk and voice-activated input (Whisper :10300), with barge-in: speaking interrupts playback and the agent.

## 7. Room features
- Search across channels, artifacts and attachments.
- Inbound webhooks (Home Assistant, GitHub, CI) that post into a channel and can wake agents.
- Channel workspace folder with a file browser panel.
- Usage dashboard: tokens, cost and speed per agent and channel (from metrics.jsonl).
- Channel export to Markdown or HTML with artifacts and images.
- Mobile push notifications for @mentions.

## 8. Channels and people (saved for later)
- A default agent for every channel, added when the channel is created (option in the create dialog).
- Private channels: password protected, hidden from everyone who is not invited.
- Channel invites for other people.
- Multiple human users (accounts, identities, per-user permissions).

## 9. Approval cards (on hold)
- Risky actions by full-access agents ask in chat first, with "allow for this session" so prompts don't repeat.

Not planned: unique notification sounds per agent in General settings (moved to the agent page instead).
