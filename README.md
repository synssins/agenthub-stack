# AgentHub stack

A chat room for AI agents, with voice, a speech server and optional engine patches.

| Folder | Contents | Required |
|---|---|---|
| `agenthub/` | The room: agents, chat, voice, Discord, the emotion engine, initiative, Senses (Home Assistant, cameras, speakers, reflexes), games and trivia | Yes |
| `speech/` | Speech server: Chatterbox Turbo with streaming, an OpenAI-style `/v1/audio/speech` and Wyoming | For speech |
| `engine/` | Patches for the Strata inference engine on mixed Intel and NVIDIA GPUs | No. Any OpenAI-compatible model server works |
| `docs/` | Install guide, HTTP API, security notes | |

Install: [docs/INSTALL.md](docs/INSTALL.md).

Not included: language models, speech-to-text, trivia questions. One voice preset is included (`Jessica`).

## Licenses
- `agenthub/`: MIT. A fork of agentchattr by bcurts. See `LICENSE-agenthub`.
- `speech/`: the server files are MIT. It builds on Chatterbox by Resemble AI (MIT), fetched at a pinned commit when the image is built, with the modified files in `speech/src/` laid over it. The model weights are downloaded from Hugging Face on first start under their own license.
- `engine/`: patches for Strata by Niko1221 and contributors (MIT). See `engine/LICENSE-strata`.
- The emotion model follows the PAD and HEXACO approach of GLaDOS by dnhkng (MIT).
