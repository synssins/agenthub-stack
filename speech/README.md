# Speech server

Chatterbox Turbo (Resemble AI) with sentence streaming, served as OpenAI `/v1/audio/speech` on port 4125 and as Wyoming TTS on port 10220. One model, one GPU, one request at a time.

```bash
docker compose up -d --build
```

- `voices/`: saved voice presets (`<Name>.pt`). `Jessica` ships as an example.
- `refs/`: reference recordings (`<Name>.wav`). A preset is made from one the first time the voice is used. Use only recordings you have the right to use; do not clone a real person's voice without their consent.
- `cache/`: the model, downloaded from Hugging Face on first start.
- `GET /api/tags` lists the emotion tags the voice performs (`[laugh]`, `[sigh]`, ...); AgentHub reads it.
- The page at `http://<host>:4125/` has the settings.

There is no login. Keep it on a trusted network.

The image clones `resemble-ai/chatterbox` at a pinned commit and lays the files of `src/` over it; see the `Dockerfile`.
