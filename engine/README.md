# Model engine (optional)

AgentHub needs only an OpenAI-compatible chat endpoint. llama.cpp, Ollama, vLLM, LM Studio or a hosted API all work, and none of this folder is required.

`strata-four-gpu/` holds 17 patches for [Strata](https://github.com/Niko1221/Strata) (MIT) that let one engine run across Intel Arc (SYCL) and NVIDIA (CUDA) GPUs in the same machine. They were written against Strata `main` at commit `477ac13cbe8e`.

```bash
git clone https://github.com/Niko1221/Strata && cd Strata
git checkout 477ac13cbe8e
git am /path/to/engine/strata-four-gpu/*.patch
```

Then build as Strata's own documentation describes for your GPUs, start its server, and add its `/v1` address under Settings > Providers > LLMs. The patches are specific to mixed Intel and NVIDIA hardware: on a single vendor, use Strata unpatched or any other server.

No model files are included. Use a model you have the right to run.
