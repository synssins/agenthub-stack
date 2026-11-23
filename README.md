# AgentHub stack

A home assistant with feelings, a voice, senses and initiative, built from parts you run yourself.

| Folder | What it is | Needed |
|---|---|---|
| `agenthub/` | The room: agents, chat, voice, Discord, the emotion engine, initiative, Senses (Home Assistant, cameras, speakers, reflexes), games and trivia | Yes |
| `speech/` | Speech server: Chatterbox Turbo with streaming, an OpenAI-style `/v1/audio/speech` and Wyoming | For a voice |
| `engine/` | Optional patches for the Strata inference engine on mixed Intel and NVIDIA GPUs | No: any OpenAI-compatible model server works |
| `docs/` | Install guide, the HTTP API, security notes | |

Start with [docs/INSTALL.md](docs/INSTALL.md).

Nothing personal ships: no models, no settings, no keys, no trivia questions, and one example voice. Every address, token and key is entered in the settings window of your own install.

## Licences
- `agenthub/`: MIT. A fork of agentchattr by bcurts; see `LICENSE-agenthub`.
- `speech/`: the server files are MIT. It builds on Chatterbox by Resemble AI (MIT), fetched at a pinned commit when the image is built, with the modified files in `speech/src/` laid over it. The model weights are downloaded from Hugging Face on first start under their own licence.
- `engine/`: patches for Strata by Niko1221 and contributors (MIT); see `engine/LICENSE-strata`.
- The emotion model follows the PAD and HEXACO approach of GLaDOS by dnhkng (MIT).
