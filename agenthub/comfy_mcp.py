"""ComfyUI MCP server for AgentHub agents (stdio): run the owner's own ComfyUI workflows.

    python comfy_mcp.py      (registered in Settings > MCP servers as a stdio server)

Workflows come from ComfyUI itself: every workflow saved in its editor (user/default/workflows) is
listed and runnable, so a new or edited workflow shows up on the next call. Editor-format files are
converted with the Nexus converter (services/comfy_mcp/uiconvert.py). API-format files in
COMFY_WORKFLOWS are added too, when ComfyUI has no workflow of the same name.

Environment:
  COMFYUI_URL      default http://127.0.0.1:8188
  COMFY_WORKFLOWS  extra folder of API-format .json files (default /data/hermes/comfy-workflows)
  COMFY_OUT        where outputs are saved (default ./generated next to this file)
  NEXUS_DIR        Nexus checkout holding the converter (default /data/nexus/current)

Inputs are found by node titles (_meta.title) and graph links: prompt -> the positive prompt /
instruction node(s), negative -> nodes titled "negative", images -> LoadImage (and video/audio
loader) nodes in order, plus seed, size and steps. Anything else: `set`
({"<node id or title>": {"<input>": value}}). Outputs are saved to files and returned as
[attach: <path>] lines, which AgentHub shows as images in the chat.
"""

from __future__ import annotations

import copy
import json
import os
import random
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import FastMCP

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)  # stdio server: keep stderr quiet
log = logging.getLogger("comfy_mcp")

COMFY = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
WORKFLOWS = Path(os.environ.get("COMFY_WORKFLOWS", "/data/hermes/comfy-workflows"))
OUT = Path(os.environ.get("COMFY_OUT", Path(__file__).with_name("generated")))
NEXUS = os.environ.get("NEXUS_DIR", "/data/nexus/current")
# Owner-editable descriptions per workflow (content tag, model, best use, hidden) + a selection guide
CATALOG = Path(os.environ.get("COMFY_CATALOG", Path(__file__).with_name("data") / "comfy_catalog.json"))
MODEL_KEYS = ("ckpt_name", "unet_name", "lora_name", "model_name", "control_net_name", "model")
RUN_TIMEOUT = 20 * 60  # model loads on the T4 can take minutes
TEXT_KEYS = ("text", "prompt", "text_g", "text_l", "value", "tags")  # value: PrimitiveString*, tags: ACE-Step
SKIP_TITLES = re.compile(r"refin|template|random|wildcard", re.I)  # helper strings, not the prompt
INPUT_NODES = ("LoadImage", "LoadImageMask", "VHS_LoadVideo", "LoadVideo", "LoadAudio")
FILE_KEYS = ("image", "video", "audio", "file")
MEDIA_KEYS = ("images", "gifs", "videos", "audio")
OUTPUT_KINDS = (("Audio", "audio"), ("Video", "video"), ("Animated", "video"), ("Image", "image"))

mcp = FastMCP("comfyui", instructions=(
    "Generate and edit images, video and music with the owner's ComfyUI workflows. Call "
    "list_workflows to see what each one does, describe_workflow for its notes and settings, then "
    "run_workflow. Edit, image-to-image, inpaint and upscale workflows need input image paths (chat "
    "attachments are on disk at the paths shown). Put the [attach: ...] lines from the result in "
    "your reply so the images appear in the chat. An image named masked-*.png carries the area to "
    "change as transparency: run an inpaint workflow with that file."))


# ------------------------------------------------------------------ workflows

def _converter():
    # shortcut: borrows the Nexus converter at runtime; vendor it here if Nexus moves or changes its API
    if NEXUS not in sys.path:
        sys.path.insert(0, NEXUS)
    from services.comfy_mcp.uiconvert import ui_to_api
    return ui_to_api


def _catalog(client: httpx.Client) -> dict[str, tuple[str, str]]:
    """name -> ("comfy", userdata path) or ("file", path). ComfyUI's own saved workflows win."""
    out: dict[str, tuple[str, str]] = {}
    try:
        r = client.get(f"{COMFY}/api/userdata", params={"dir": "workflows", "recurse": "true"})
        r.raise_for_status()
        for rel in r.json():
            if rel.endswith(".json") and not rel.startswith("_"):  # _archive/ etc.
                out[Path(rel).stem] = ("comfy", rel)
    except httpx.HTTPError:
        pass  # ComfyUI down: the folder still lists, runs will fail with ComfyUI's error
    for p in sorted(WORKFLOWS.glob("*.json")):
        out.setdefault(p.stem, ("file", str(p)))
    return dict(sorted(out.items()))


class Loader:
    """Loads workflows as API graphs (+ their notes) for one tool call; object_info is fetched once."""

    def __init__(self, client: httpx.Client):
        self.client, self._info = client, None

    def info(self) -> dict:
        if self._info is None:
            self._info = self.client.get(f"{COMFY}/object_info").raise_for_status().json()
        return self._info

    def load(self, src: tuple[str, str]) -> tuple[dict, str]:
        """(API graph, notes). Raises ValueError when the workflow can't be run as saved."""
        kind, ref = src
        if kind == "comfy":
            r = self.client.get(f"{COMFY}/api/userdata/" + quote("workflows/" + ref, safe=""))
            data = r.raise_for_status().json()
        else:
            data = json.loads(Path(ref).read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):  # editor format
            notes = "\n\n".join(str((n.get("widgets_values") or [""])[0]) for n in data["nodes"]
                                 if n.get("type") in ("Note", "MarkdownNote"))
            graph, report = _converter()(data, self.info())
            if not report.get("clean"):
                raise ValueError("can't run as saved: " + json.dumps(
                    report.get("problems") or report.get("skipped"))[:600])
            return graph, notes
        notes = data.get("description", "") if isinstance(data, dict) else ""
        g = data.get("graph", data) if isinstance(data, dict) else {}  # Nexus presets wrap the graph
        return {k: v for k, v in g.items() if isinstance(v, dict) and "class_type" in v}, notes


def _title(node: dict) -> str:
    return (node.get("_meta") or {}).get("title", "") or node.get("class_type", "")


def _linked(graph: dict, input_name: str) -> set[str]:
    """Node ids feeding `input_name` (e.g. a sampler's positive/negative) anywhere in the graph."""
    return {str(v["inputs"][input_name][0]) for v in graph.values()
            if isinstance(v.get("inputs", {}).get(input_name), list)}


def _text_nodes(graph: dict) -> tuple[list[str], list[str]]:
    """(positive ids, negative ids): prompt nodes with an editable text input."""
    pos, neg = [], []
    linked_neg = _linked(graph, "negative")
    for nid, n in graph.items():
        if not any(isinstance(n["inputs"].get(k), str) for k in TEXT_KEYS):
            continue
        t = _title(n).lower()
        if SKIP_TITLES.search(t) or ("value" in n["inputs"] and not n["class_type"].startswith("PrimitiveString")):
            continue
        if "negative" in t or nid in linked_neg:
            neg.append(nid)
        elif re.search(r"prompt|instruction|positive|caption|description|text|encode", t):
            pos.append(nid)
    return pos, neg


def _image_nodes(graph: dict) -> list[str]:
    ids = [nid for nid, n in graph.items() if n["class_type"] in INPUT_NODES]
    return sorted(ids, key=lambda i: (int(i) if i.isdigit() else 1 << 30, i))


def _outputs(graph: dict) -> list[str]:
    kinds = []
    for n in graph.values():
        c = n["class_type"]
        if c.startswith("Save") or c == "VHS_VideoCombine":
            k = "video" if c == "VHS_VideoCombine" else next((v for key, v in OUTPUT_KINDS if key in c), "file")
            if k not in kinds:
                kinds.append(k)
    return kinds


def _about(notes: str, limit: int = 220) -> str:
    """First useful sentence(s) of the workflow's notes."""
    text = re.sub(r"[#*_`>]+", "", notes)
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _descriptions() -> dict:
    try:
        return json.loads(CATALOG.read_text("utf-8"))
    except (OSError, ValueError):
        return {}  # no catalog: workflows still list with their own notes


def _models(graph: dict) -> list[str]:
    """Model files the workflow loads (checkpoints, diffusion models, LoRAs, upscalers)."""
    out = []
    for n in graph.values():
        for k in MODEL_KEYS:
            v = n["inputs"].get(k)
            if isinstance(v, str) and "." in v and v not in out:
                out.append(v)
    return out


def _summary(name: str, graph: dict, notes: str, info: dict | None = None) -> str:
    info = info or {}
    pos, neg = _text_nodes(graph)
    imgs = _image_nodes(graph)
    takes = (["prompt"] if pos else []) + (["negative"] if neg else [])
    if imgs:
        takes.append(f"{len(imgs)} input file(s): " + "; ".join(_title(graph[i]) for i in imgs))
    if any("Latent" in n["class_type"] and isinstance(n["inputs"].get("width"), int) for n in graph.values()):
        takes.append("width/height")
    tag = f" | {info['content'].upper()}" if info.get("content") else ""
    line = f"- {name} -> {'/'.join(_outputs(graph)) or 'output'}{tag} | takes: {', '.join(takes) or 'nothing'}"
    line += f"\n    model: {info.get('model') or ', '.join(_models(graph)) or '?'}"
    if info.get("best_for"):
        return line + f"\n    best for: {info['best_for']}"
    return line + (f"\n    {_about(notes)}" if notes.strip() else "")


def _resolve(catalog: dict, workflow: str) -> str:
    if workflow in catalog:
        return workflow
    hits = [k for k in catalog if workflow.lower() in k.lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"workflow '{workflow}' " + ("matches several: " + "; ".join(hits) if hits
                                                  else "not found; call list_workflows"))


# ------------------------------------------------------------------ patching

def _patch(graph: dict, prompt: str, negative: str, image_names: list[str], width: int, height: int,
           seed: int, steps: int, overrides: dict) -> dict:
    g = copy.deepcopy(graph)
    pos, neg = _text_nodes(g)
    for ids, value in ((pos, prompt), (neg, negative)):
        if value:
            for nid in ids:
                key = next(k for k in TEXT_KEYS if isinstance(g[nid]["inputs"].get(k), str))
                g[nid]["inputs"][key] = value
    for nid, name in zip(_image_nodes(g), image_names):
        key = next(k for k in FILE_KEYS if isinstance(g[nid]["inputs"].get(k), str))
        g[nid]["inputs"][key] = name
    seed = seed if seed >= 0 else random.randint(0, 2**48)
    for n in g.values():
        ins = n["inputs"]
        for k in ("seed", "noise_seed"):
            if isinstance(ins.get(k), int):
                ins[k] = seed
        if steps and isinstance(ins.get("steps"), int):
            ins["steps"] = steps
        if "Latent" in n["class_type"] and isinstance(ins.get("width"), int):
            if width:
                ins["width"] = width
            if height:
                ins["height"] = height
    for key, values in (overrides or {}).items():
        nid = key if key in g else next((i for i, n in g.items() if _title(n).lower() == str(key).lower()), None)
        if nid is None:
            raise ValueError(f"set: no node with id or title '{key}'")
        g[nid]["inputs"].update(values)
    return g


# ------------------------------------------------------------------ ComfyUI

def _upload(client: httpx.Client, path: str) -> str:
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise ValueError(f"image not found: {path}")
    with p.open("rb") as f:
        r = client.post(f"{COMFY}/upload/image", files={"image": (f"{uuid.uuid4().hex[:8]}_{p.name}", f)},
                        data={"overwrite": "true"})
    r.raise_for_status()
    d = r.json()
    return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]


_pending: set[str] = set()  # prompts this process queued and still waits on


def _cancel(pids) -> None:
    """Drop prompts from ComfyUI's queue and interrupt one that is running, so a timed-out or
    abandoned call doesn't leave the GPU busy (and its output in ComfyUI's folder)."""
    pids = set(pids)
    if not pids:
        return
    try:
        with httpx.Client(timeout=5) as c:
            c.post(f"{COMFY}/queue", json={"delete": sorted(pids)})
            running = {r[1] for r in c.get(f"{COMFY}/queue").json().get("queue_running", [])}
            for pid in pids & running:
                c.post(f"{COMFY}/interrupt", json={"prompt_id": pid})
    except Exception as exc:  # best effort: ComfyUI may be down
        log.warning("could not cancel ComfyUI prompts %s: %s", pids, exc)


def _cancel_pending(*_):
    _cancel(list(_pending))
    _pending.clear()


def _run(client: httpx.Client, graph: dict) -> dict:
    r = client.post(f"{COMFY}/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex})
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI refused the workflow: {r.text[:1500]}")
    pid = r.json()["prompt_id"]
    _pending.add(pid)
    try:
        deadline = time.monotonic() + RUN_TIMEOUT
        while time.monotonic() < deadline:
            h = client.get(f"{COMFY}/history/{pid}").json().get(pid)
            if h and h.get("status", {}).get("completed") or (h and h.get("status", {}).get("status_str") == "error"):
                if h["status"].get("status_str") == "error":
                    msgs = [m for m in h["status"].get("messages", []) if m[0] == "execution_error"]
                    raise RuntimeError(f"ComfyUI error: {json.dumps(msgs)[:1500]}")
                return h
            time.sleep(1)
        _cancel([pid])
        raise TimeoutError(f"ComfyUI did not finish within {RUN_TIMEOUT // 60} minutes (prompt {pid}); cancelled it")
    finally:
        _pending.discard(pid)


def _save_outputs(client: httpx.Client, history: dict) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    saved = []
    for node_out in history.get("outputs", {}).values():
        for key in MEDIA_KEYS:
            for item in node_out.get(key, []):
                if item.get("type") != "output":
                    continue  # previews and temp files
                r = client.get(f"{COMFY}/view", params={k: item.get(k, "") for k in ("filename", "subfolder", "type")})
                r.raise_for_status()
                dest = OUT / f"{uuid.uuid4().hex[:8]}_{item['filename']}"
                dest.write_bytes(r.content)
                saved.append(dest)
    return saved


# ------------------------------------------------------------------ tools

@mcp.tool()
def list_workflows(search: str = "", content: str = "") -> str:
    """List the owner's ComfyUI workflows with what each one outputs, its content tag (NSFW = made for
    adult content, NSFW-CAPABLE, SFW-LEANING, N/A), the model it uses, what it takes and what it is
    best for, plus a short guide for choosing. Always current: new workflows saved in ComfyUI appear.
    search: filter by name ("edit", "inpaint", "text to image", "upscale", "video", "big love").
    content: filter by tag ("nsfw", "nsfw-capable", "sfw-leaning")."""
    desc = _descriptions()
    lines = [desc["_guide"], ""] if desc.get("_guide") and not search and not content else []
    with httpx.Client(timeout=60) as client:
        cat, loader = _catalog(client), Loader(client)
        for name, src in cat.items():
            info = desc.get(name) or {}
            if info.get("hidden") or search.lower() not in name.lower() \
                    or (content and info.get("content", "").lower() != content.lower()):
                continue
            try:
                lines.append(_summary(name, *loader.load(src), info))
            except (ValueError, OSError, httpx.HTTPError, ImportError) as exc:
                lines.append(f"- {name} -> unavailable ({str(exc)[:160]})")
    return "\n".join(lines) or "No workflows match."


@mcp.tool()
def describe_workflow(workflow: str) -> str:
    """Full notes for one workflow plus every setting it has (node id, title, current values), so you
    can pick the right inputs and use run_workflow's `set` for anything beyond prompt/images/size."""
    try:
        with httpx.Client(timeout=60) as client:
            cat = _catalog(client)
            name = _resolve(cat, workflow)
            graph, notes = Loader(client).load(cat[name])
    except (ValueError, OSError, httpx.HTTPError, ImportError) as exc:
        return f"error: {exc}"
    rows = []
    for nid, n in graph.items():
        vals = {k: (v if not isinstance(v, str) or len(v) <= 80 else v[:80] + "...")
                for k, v in n["inputs"].items() if not isinstance(v, list)}
        if vals:
            rows.append(f"  {nid} '{_title(n)}' ({n['class_type']}): {json.dumps(vals, ensure_ascii=False)}")
    head = _summary(name, graph, "", _descriptions().get(name))
    files = ", ".join(_models(graph)) or "(none)"
    return (f"{head}\n    model files: {files}\n\nNotes:\n{notes.strip() or '(none)'}\n\nSettings:\n"
            + "\n".join(rows))


@mcp.tool()
def run_workflow(workflow: str, prompt: str = "", negative: str = "", images: list[str] | None = None,
                 width: int = 0, height: int = 0, seed: int = -1, steps: int = 0,
                 set: dict | None = None) -> str:
    """Run a ComfyUI workflow and return its outputs as [attach: <path>] lines.

    workflow: name from list_workflows (a unique part of the name is enough).
    prompt: what to make, or the edit instruction for edit workflows. Empty keeps the workflow's own.
    negative: things to avoid (optional). images: input file paths (images, or a video/audio file
    for video/audio workflows) in the order list_workflows shows. Inpaint workflows use the image's
    alpha channel as the mask; an image named masked-*.png carries the area to change as
    transparency, so pass it to an inpaint workflow.
    width/height/steps: optional; 0 keeps the workflow's values. seed: -1 = random.
    set: advanced overrides {"<node id or title>": {"<input name>": value}} (see describe_workflow).
    """
    try:
        started = time.monotonic()
        with httpx.Client(timeout=60) as client:
            cat = _catalog(client)
            name = _resolve(cat, workflow)
            graph, _ = Loader(client).load(cat[name])
            needed = _image_nodes(graph)
            images = images or []
            if len(images) < len(needed):
                return (f"error: '{name}' needs {len(needed)} input file(s): "
                        + "; ".join(_title(graph[i]) for i in needed))
            uploaded = [_upload(client, p) for p in images[:len(needed)]]
            g = _patch(graph, prompt, negative, uploaded, width, height, seed, steps, set or {})
            files = _save_outputs(client, _run(client, g))
        if not files:
            return f"error: '{name}' finished but produced no saved outputs"
        took = time.monotonic() - started
        return (f"{name}: {len(files)} output(s) in {took:.0f}s.\n"
                + "\n".join(f"[attach: {f}]" for f in files))
    except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError, ImportError) as exc:
        return f"error: {exc}"


def _mask_graph(image: str, target: str, grow: int, invert: bool) -> dict:
    """Florence-2 finds `target` (phrase grounding -> boxes), SAM2 outlines it, and the mask becomes
    the image's transparency: the format the inpaint workflows read their mask from."""
    mask = ["7", 0]
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "DownloadAndLoadFlorence2Model",
              "inputs": {"model": "microsoft/Florence-2-large", "precision": "fp16"}},
        "3": {"class_type": "Florence2Run", "inputs": {
            "image": ["1", 0], "florence2_model": ["2", 0], "text_input": target,
            "task": "caption_to_phrase_grounding", "fill_mask": True, "keep_model_loaded": False,
            "max_new_tokens": 1024, "num_beams": 3, "do_sample": False, "output_mask_select": "", "seed": 1}},
        "4": {"class_type": "Florence2toCoordinates", "inputs": {"data": ["3", 3], "index": "", "batch": False}},
        "5": {"class_type": "DownloadAndLoadSAM2Model", "inputs": {
            "model": "sam2.1_hiera_large.safetensors", "segmentor": "single_image", "device": "cuda",
            "precision": "fp16"}},
        "6": {"class_type": "Sam2Segmentation", "inputs": {
            "sam2_model": ["5", 0], "image": ["1", 0], "bboxes": ["4", 1], "keep_model_loaded": False,
            "individual_objects": True}},  # one mask per box, OR-ed; False keeps only one object
        "7": {"class_type": "GrowMask", "inputs": {"mask": ["6", 0], "expand": grow, "tapered_corners": True}},
    }
    if invert:
        g["8"] = {"class_type": "InvertMask", "inputs": {"mask": ["7", 0]}}
        mask = ["8", 0]
    g["9"] = {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["1", 0], "alpha": mask}}  # alpha = 1 - mask
    g["10"] = {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "agenthub/masked"}}
    return g


@mcp.tool()
def make_mask(image: str, target: str, grow: int = 8, invert: bool = False) -> str:
    """Mark an object in an image as the area to change, from words: e.g. target="the wheels",
    "the bicycle frame", "her hair", "the sky". Returns a copy of the image whose transparent area is
    the mask; pass that file to an inpaint workflow with run_workflow(images=[...]).
    grow: pixels to widen the mask (default 8). invert: mask everything EXCEPT the target.
    Works on objects, not on spatial phrases like "the left half"; for those use an instruction-edit
    workflow (e.g. KreaEdit2, Gwen3) with the phrase in the prompt instead."""
    if not target.strip():
        return "error: say what to mask, e.g. target='the wheels'"
    try:
        with httpx.Client(timeout=60) as client:
            g = _mask_graph(_upload(client, image), target.strip(), int(grow), bool(invert))
            files = [f for f in _save_outputs(client, _run(client, g)) if f.suffix.lower() == ".png"]
    except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        return f"error: {exc}"
    if not files:
        return f"error: nothing matching '{target}' was found in the image"
    return (f"Mask for '{target}'{' (inverted)' if invert else ''}: transparent area = the part to change.\n"
            f"[attach: {files[0]}]")


if __name__ == "__main__":
    import atexit
    import signal
    # The hub ends this process when a call times out or the agent is stopped: cancel what we queued
    atexit.register(_cancel_pending)
    signal.signal(signal.SIGTERM, lambda *_: (_cancel_pending(), os._exit(0)))
    mcp.run()
