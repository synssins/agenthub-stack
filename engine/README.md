# Model engine (optional)

AgentHub needs an OpenAI-compatible chat endpoint. llama.cpp, Ollama, vLLM, LM Studio and hosted APIs work without this folder.

`strata-four-gpu/` holds 17 patches for [Strata](https://github.com/Niko1221/Strata) (MIT) that let one engine run across Intel Arc (SYCL) and NVIDIA (CUDA) GPUs in the same machine. They were written against Strata `main` at commit `477ac13cbe8e`.

```bash
git clone https://github.com/Niko1221/Strata && cd Strata
git checkout 477ac13cbe8e
git am /path/to/engine/strata-four-gpu/*.patch
```

Build as the Strata documentation describes, start its server, and add its `/v1` address under Settings > Providers > LLMs. The patches apply to mixed Intel and NVIDIA hardware only. With GPUs of one vendor, use Strata unpatched or another server.

Model files are not included.
