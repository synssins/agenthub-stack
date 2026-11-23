# Speech server

Chatterbox Turbo (Resemble AI) with sentence streaming, served as OpenAI `/v1/audio/speech` on port 4125 and as Wyoming TTS on port 10220. One model, one GPU, one request at a time.

```bash
docker compose up -d --build
```

- `voices/`: saved voice presets (`<Name>.pt`). `Jessica` is included.
- `refs/`: reference recordings (`<Name>.wav`). A preset is made from a recording the first time its voice is used.
- `cache/`: the model, downloaded from Hugging Face on first start.
- `GET /api/tags` lists the emotion tags the voice performs, such as `[laugh]` and `[sigh]`. AgentHub reads this list.
- The page at `http://<host>:4125/` has the settings.

The server has no login. Run it on a trusted network.

The image clones `resemble-ai/chatterbox` at a pinned commit and lays the files of `src/` over it. See the `Dockerfile`.
