import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import comfy_mcp as c


def node(cls, title, **inputs):
    return {"class_type": cls, "inputs": inputs, "_meta": {"title": title}}


EDIT = {  # shaped like the owner's KreaEdit2 / Gwen3 edit workflows
    "2": node("LoadImage", "Source image", image="example.png"),
    "3": node("PrimitiveStringMultiline", "Edit instruction", value="make it red"),
    "9": node("StringConcatenate", "Krea 2 edit template (start)", string_a="<|im_start|>", string_b=["3", 0]),
    "10": node("PrimitiveStringMultiline", "Refining prompt (added before)", value="skin detail"),
    "11": node("CLIPTextEncode", "Negative prompt", text="blurry"),
    "12": node("KSampler", "Sampler", seed=0, steps=6, positive=["9", 0], negative=["11", 0]),
    "13": node("EmptyLatentImage", "Size", width=1024, height=1024, batch_size=1),
    "14": node("SaveImage", "Save image", filename_prefix="x", images=["12", 0]),
}


class ComfyWorkflowInputs(unittest.TestCase):
    def test_finds_instruction_negative_and_images_but_not_helper_strings(self):
        pos, neg = c._text_nodes(EDIT)
        self.assertEqual(pos, ["3"])        # not the template or the refining prompt
        self.assertEqual(neg, ["11"])
        self.assertEqual(c._image_nodes(EDIT), ["2"])

    def test_patch_sets_prompt_images_seed_size_steps_and_overrides(self):
        g = c._patch(EDIT, "a blue hat", "lowres", ["up_1.png"], 832, 640, 7, 4,
                     {"Refining prompt (added before)": {"value": "film grain"}})
        self.assertEqual(g["3"]["inputs"]["value"], "a blue hat")
        self.assertEqual(g["11"]["inputs"]["text"], "lowres")
        self.assertEqual(g["2"]["inputs"]["image"], "up_1.png")
        self.assertEqual((g["12"]["inputs"]["seed"], g["12"]["inputs"]["steps"]), (7, 4))
        self.assertEqual((g["13"]["inputs"]["width"], g["13"]["inputs"]["height"]), (832, 640))
        self.assertEqual(g["10"]["inputs"]["value"], "film grain")
        self.assertEqual(EDIT["3"]["inputs"]["value"], "make it red")  # the saved workflow is untouched

    def test_empty_prompt_keeps_the_workflows_own_and_unknown_override_fails(self):
        g = c._patch(EDIT, "", "", [], 0, 0, -1, 0, {})
        self.assertEqual(g["3"]["inputs"]["value"], "make it red")
        self.assertEqual(g["13"]["inputs"]["width"], 1024)
        with self.assertRaises(ValueError):
            c._patch(EDIT, "", "", [], 0, 0, 1, 0, {"no such node": {"x": 1}})

    def test_summary_advertises_output_kind_inputs_and_notes(self):
        line = c._summary("Edit – Test", EDIT, "Instruction edit. Inputs to change: the source image.")
        self.assertIn("-> image", line)
        self.assertIn("prompt, negative, 1 input file(s): Source image", line)
        self.assertIn("Instruction edit.", line)

    def test_resolve_accepts_a_unique_part_of_the_name(self):
        cat = {"Text to Image – Photoreal (RealVisXL V5)": 1, "Image Edit – Big Love Gwen3": 2}
        self.assertEqual(c._resolve(cat, "gwen3"), "Image Edit – Big Love Gwen3")
        with self.assertRaises(ValueError):
            c._resolve(cat, "–")


class MaskGraph(unittest.TestCase):
    def test_mask_becomes_transparency_and_invert_flips_it(self):
        g = c._mask_graph("photo.png", "the wheels", 8, False)
        self.assertEqual(g["3"]["inputs"]["text_input"], "the wheels")
        self.assertTrue(g["6"]["inputs"]["individual_objects"])   # every matching object, not just one
        self.assertEqual(g["9"]["inputs"]["alpha"], ["7", 0])     # JoinImageWithAlpha: alpha = 1 - mask
        inv = c._mask_graph("photo.png", "the wheels", 8, True)
        self.assertEqual(inv["9"]["inputs"]["alpha"], ["8", 0])
        self.assertEqual(inv["8"]["class_type"], "InvertMask")



class Catalog(unittest.TestCase):
    def test_catalog_tag_model_and_best_use_replace_the_notes_excerpt(self):
        info = {"content": "nsfw", "model": "Big Love KreaEdit2", "best_for": "plain-language edits"}
        line = c._summary("Edit – Test", EDIT, "long workflow notes", info)
        self.assertIn("| NSFW |", line)
        self.assertIn("model: Big Love KreaEdit2", line)
        self.assertIn("best for: plain-language edits", line)
        self.assertNotIn("long workflow notes", line)

    def test_uncatalogued_workflow_still_shows_its_model_files_and_notes(self):
        g = dict(EDIT, m=node("UNETLoader", "Model", unet_name="BigLoveKreaEdit2_fp8mixed.safetensors"))
        line = c._summary("New one", g, "Fresh notes.")
        self.assertIn("model: BigLoveKreaEdit2_fp8mixed.safetensors", line)
        self.assertIn("Fresh notes.", line)


class Cancel(unittest.TestCase):
    def test_timeout_cancels_the_prompt_it_queued(self):
        from unittest import mock

        class Resp:
            def __init__(self, data, code=200):
                self.status_code, self._d, self.text = code, data, ""
            def json(self):
                return self._d

        class Client:
            def __init__(self, *a, **k):
                self.posts = []
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def post(self, url, json=None):
                self.posts.append((url.rsplit("/", 1)[1], json))
                return Resp({"prompt_id": "p1"})
            def get(self, url):
                return Resp({"queue_running": [[0, "p1"]]} if url.endswith("/queue") else {})

        canceller = Client()
        with mock.patch.object(c, "RUN_TIMEOUT", 0), mock.patch.object(c.httpx, "Client", lambda **k: canceller):
            with self.assertRaises(TimeoutError):
                c._run(Client(), {})
        self.assertIn(("queue", {"delete": ["p1"]}), canceller.posts)
        self.assertIn(("interrupt", {"prompt_id": "p1"}), canceller.posts)
        self.assertEqual(c._pending, set())


if __name__ == "__main__":
    unittest.main()
