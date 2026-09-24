"""SAM 3 inference engine on Apple Silicon (MLX).

Runs inside the backend venv, never inside Krita. Model definitions and weights
come from mlx-vlm's SAM 3 port; the interactive (click / box) path is
implemented here so that it matches the reference SAM 2/3 tracker decoder
(Hugging Face ``Sam3TrackerModel``), which the mlx-vlm video helpers deviate
from in a few places:

* prompts are normalised by the 1008 px model input (not the 72 px feature
  grid), shifted to pixel centres, and padded with a "not a point" token;
* ``no_memory_embedding`` is added to the stride-14 features (image mode);
* the high-resolution skip features are added *before* the upscaling
  norm/activation, as in the reference decoder;
* the dense positional encoding samples feature-cell centres.

All coordinates crossing the engine boundary are normalised to ``[0, 1]``
relative to the full document, so the caller never needs to know the model
resolution. The image is squashed (not letterboxed) to 1008 x 1008 exactly
like the reference processor, which makes that mapping a pure scale.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
import numpy as np

MODEL_REPO = "mlx-community/sam3-bf16"
MODEL_REVISION = "dfe573c3171dbcfda8399c650d9135afa7e94592"
MODEL_BYTES = 1_724_000_000  # approximate download size, for progress reporting
IMAGE_SIZE = 1008
LOW_RES = 288  # tracker / detector mask logits resolution (IMAGE_SIZE / 14 * 4)
TEXT_LEN = 32


def _log(msg: str) -> None:
    import sys

    print(f"[engine] {msg}", file=sys.stderr, flush=True)


def _patch_rope_dtype() -> None:
    """Keep the ViT residual stream in bf16.

    mlx-vlm precomputes RoPE tables in float32, which silently promotes q/k -
    and then every following op - to float32. Rotate in float32 for accuracy
    but hand the result back in the input dtype.
    """
    import mlx_vlm.models.sam3.vision as vision
    from mlx_vlm.models.sam3.position import rotate_pairwise

    def apply_rotary_keep_dtype(xq, xk, cos, sin):
        f32 = mx.float32
        q = xq.astype(f32) * cos + rotate_pairwise(xq).astype(f32) * sin
        k = xk.astype(f32) * cos + rotate_pairwise(xk).astype(f32) * sin
        return q.astype(xq.dtype), k.astype(xk.dtype)

    vision.apply_rotary_enc = apply_rotary_keep_dtype


@dataclass
class ImageState:
    key: str
    # Tracker (interactive) features.
    image_embed: mx.array  # (1, 72*72, 256) stride-14 features + no-memory embedding
    feat_s0: mx.array  # (1, 288, 288, 32) projected high-res skip
    feat_s1: mx.array  # (1, 144, 144, 64) projected high-res skip
    backbone: mx.array  # (1, 72, 72, 1024) shared ViT output, reused by the detector
    detector: tuple | None = None  # lazily computed (src, pos, fpn_levels, (h, w))
    encode_ms: float = 0.0


@dataclass
class MaskResult:
    logits: np.ndarray | None  # (LOW_RES, LOW_RES) float32, or None when nothing was found
    score: float = 0.0
    count: int = 0


class Sam3Engine:
    def __init__(self, repo: str = MODEL_REPO, revision: str | None = MODEL_REVISION, cache_images: int = 3):
        self.repo = repo
        self.revision = revision
        self.max_images = cache_images
        self.images: OrderedDict[str, ImageState] = OrderedDict()
        self.text_embeds: OrderedDict[str, tuple] = OrderedDict()
        self.model = None
        self.tokenizer = None
        self.model_path: Path | None = None

    # ------------------------------------------------------------------ loading

    def is_cached(self) -> bool:
        from huggingface_hub import snapshot_download

        try:
            snapshot_download(self.repo, revision=self.revision, local_files_only=True)
            return True
        except Exception:
            return False

    @staticmethod
    def download(repo: str = MODEL_REPO, revision: str | None = MODEL_REVISION, progress=None) -> Path:
        """Fetch weights into the Hugging Face cache (no-op when present)."""
        from huggingface_hub import snapshot_download

        try:
            return Path(snapshot_download(repo, revision=revision, local_files_only=True))
        except Exception:
            pass
        if progress:
            progress("downloading", 0.0)
        path = Path(snapshot_download(repo, revision=revision))
        if progress:
            progress("downloading", 1.0)
        return path

    def load(self, progress=None) -> None:
        from mlx_vlm.utils import load_model
        from transformers import CLIPTokenizerFast

        _patch_rope_dtype()
        self.model_path = self.download(self.repo, self.revision, progress)
        if progress:
            progress("loading", 0.0)
        t0 = time.perf_counter()
        self.model = load_model(self.model_path)
        self.model.eval()
        self.tokenizer = CLIPTokenizerFast.from_pretrained(str(self.model_path))
        tracker = self.model.tracker_model
        pe = tracker.prompt_encoder
        grid = pe.image_embedding_size[0]
        # Dense PE sampled at feature-cell centres, shape (1, HW, 256).
        centers = (mx.arange(grid, dtype=mx.float32) + 0.5) / grid
        gy, gx = mx.meshgrid(centers, centers, indexing="ij")
        coords = mx.stack([gx.reshape(-1), gy.reshape(-1)], axis=-1)[None]
        self._dense_pe = pe.shared_embedding.forward_with_coords(coords)
        self._grid = grid
        mx.eval(self._dense_pe)
        _log(f"model loaded in {time.perf_counter() - t0:.1f}s from {self.model_path}")
        if progress:
            progress("warming up", 0.0)
        self.warmup()

    def warmup(self) -> None:
        t0 = time.perf_counter()
        rgb = np.full((IMAGE_SIZE, IMAGE_SIZE, 3), 127, np.uint8)
        state = self.set_image("__warmup__", rgb)
        # Touch every path once so MLX compiles its kernels before the first real prompt.
        self.predict_point(state, [(0.5, 0.5, 1)])
        self.predict_box(state, (0.25, 0.25, 0.75, 0.75))
        self.predict_objects_in_region(state, [(0.2, 0.1), (0.9, 0.3), (0.7, 0.9), (0.1, 0.7)])
        self.predict_text(state, "object")
        render_mask(np.full((LOW_RES, LOW_RES), 5.0, np.float32), 64, 64)
        self.images.pop("__warmup__", None)
        _log(f"warmup {time.perf_counter() - t0:.2f}s")

    # ------------------------------------------------------------------ images

    @staticmethod
    def image_key(rgb: np.ndarray) -> str:
        return hashlib.blake2b(rgb.tobytes(), digest_size=16).hexdigest()

    def has_image(self, key: str) -> bool:
        return key in self.images

    def get_image(self, key: str) -> ImageState:
        state = self.images[key]
        self.images.move_to_end(key)
        return state

    def set_image(self, key: str, rgb: np.ndarray) -> ImageState:
        """Encode a (1008, 1008, 3) uint8 RGB image and cache its features."""
        if key in self.images:
            return self.get_image(key)
        if rgb.shape != (IMAGE_SIZE, IMAGE_SIZE, 3):
            raise ValueError(f"expected {IMAGE_SIZE}x{IMAGE_SIZE} RGB, got {rgb.shape}")
        t0 = time.perf_counter()
        m = self.model
        pixels = mx.array(rgb).astype(mx.bfloat16) * (2.0 / 255.0) - 1.0  # (x/255 - .5) / .5
        backbone = m.detector_model.vision_encoder.backbone(pixels[None])
        fpn = m.tracker_neck(backbone)  # [288, 144, 72, 36]
        decoder = m.tracker_model.mask_decoder
        feat_s0 = decoder.conv_s0(fpn[0])
        feat_s1 = decoder.conv_s1(fpn[1])
        embed = fpn[2] + m.tracker_model.no_memory_embedding.reshape(1, 1, 1, -1)
        embed = embed.reshape(1, -1, embed.shape[-1])
        mx.eval(backbone, feat_s0, feat_s1, embed)
        state = ImageState(key, embed, feat_s0, feat_s1, backbone)
        state.encode_ms = (time.perf_counter() - t0) * 1000
        self.images[key] = state
        while len(self.images) > self.max_images:
            self.images.popitem(last=False)
        return state

    # ------------------------------------------------------------ interactive

    def _embed_prompts(self, points=None, boxes=None) -> mx.array:
        """Sparse prompt tokens, matching Sam3TrackerPromptEncoder.

        points: (B, N, 3) array of (x, y, label) in model pixels; boxes: (B, 4).
        """
        pe = self.model.tracker_model.prompt_encoder
        not_a_point = pe.not_a_point_embed.weight  # (1, D)
        tokens = []
        if points is not None:
            pts = mx.array(points[..., :2], dtype=mx.float32) + 0.5
            labels = mx.array(points[..., 2].astype(np.int32))
            if boxes is None:  # pad with a "not a point" token
                pts = mx.concatenate([pts, mx.zeros((pts.shape[0], 1, 2))], axis=1)
                labels = mx.concatenate([labels, -mx.ones((labels.shape[0], 1), dtype=mx.int32)], axis=1)
            emb = pe.shared_embedding.forward_with_coords(pts / IMAGE_SIZE)
            is_pad = (labels == -1)[..., None]
            emb = mx.where(is_pad, not_a_point, emb)
            emb = emb + pe.point_embed(mx.maximum(labels, 0)) * (labels >= 0)[..., None]
            tokens.append(emb)
        if boxes is not None:
            corners = mx.array(boxes, dtype=mx.float32).reshape(-1, 2, 2) + 0.5
            emb = pe.shared_embedding.forward_with_coords(corners / IMAGE_SIZE)
            w = pe.point_embed.weight
            emb = mx.concatenate(
                [emb[:, 0:1] + w[2], emb[:, 1:2] + w[3], mx.broadcast_to(not_a_point[None], (emb.shape[0], 1, emb.shape[-1]))],
                axis=1,
            )
            tokens.append(emb)
        return mx.concatenate(tokens, axis=1) if len(tokens) > 1 else tokens[0]

    def _decode(self, state: ImageState, sparse: mx.array, multimask: bool):
        """Reference SAM 2/3 mask decoder. Returns (masks (B,K,288,288), iou (B,K))."""
        md = self.model.tracker_model.mask_decoder
        B = sparse.shape[0]
        d = state.image_embed.shape[-1]
        hw = state.image_embed.shape[1]
        g = self._grid
        out_tokens = mx.concatenate([md.iou_token.weight, md.mask_tokens.weight, md.obj_score_token.weight], axis=0)
        tokens = mx.concatenate([mx.broadcast_to(out_tokens[None], (B,) + out_tokens.shape), sparse.astype(out_tokens.dtype)], axis=1)
        no_mask = self.model.tracker_model.prompt_encoder.no_mask_embed.weight.reshape(1, 1, d)
        src = mx.broadcast_to(state.image_embed + no_mask, (B, hw, d))
        image_pe = mx.broadcast_to(self._dense_pe, (B, hw, d)).astype(src.dtype)
        hs, src = md.transformer(src, image_pe, tokens)
        iou_out = hs[:, 0]
        mask_out = hs[:, 1 : 1 + md.num_mask_tokens]

        src = src.reshape(B, g, g, d)
        up = md.upscale_conv1(src) + state.feat_s1
        up = nn.gelu(md.upscale_layer_norm(up))
        up = nn.gelu(md.upscale_conv2(up) + state.feat_s0)  # (B, 288, 288, 32)
        hyper = mx.stack([mlp(mask_out[:, i]) for i, mlp in enumerate(md.output_hypernetworks_mlps)], axis=1)
        h, w, c = up.shape[1:]
        masks = (hyper @ up.reshape(B, h * w, c).transpose(0, 2, 1)).reshape(B, -1, h, w)
        iou = mx.sigmoid(md.iou_prediction_head(iou_out).astype(mx.float32))  # reference uses sigmoid_output
        if multimask:
            return masks[:, 1:], iou[:, 1:]
        return masks[:, :1], iou[:, :1]

    def predict_point(self, state: ImageState, points, multimask: bool = True) -> MaskResult:
        """points: iterable of (u, v, label) with u, v normalised to the document."""
        pts = np.array([[(u * IMAGE_SIZE, v * IMAGE_SIZE, lbl) for u, v, lbl in points]], np.float32)
        multimask = multimask and len(pts[0]) == 1
        masks, iou = self._decode(state, self._embed_prompts(points=pts), multimask)
        mx.eval(masks, iou)
        iou = np.array(iou[0].astype(mx.float32))
        best = int(np.argmax(iou))
        return MaskResult(np.array(masks[0, best].astype(mx.float32)), float(iou[best]), 1)

    def predict_box(self, state: ImageState, box, points=None) -> MaskResult:
        b = np.array([box], np.float32) * IMAGE_SIZE
        pts = None
        if points:
            pts = np.array([[(u * IMAGE_SIZE, v * IMAGE_SIZE, lbl) for u, v, lbl in points]], np.float32)
        masks, iou = self._decode(state, self._embed_prompts(points=pts, boxes=b), multimask=False)
        mx.eval(masks, iou)
        return MaskResult(np.array(masks[0, 0].astype(mx.float32)), float(iou[0, 0]), 1)

    def predict_objects_in_box(self, state: ImageState, box, **kw) -> MaskResult:
        u0, v0, u1, v1 = box
        return self.predict_objects_in_region(state, [(u0, v0), (u1, v0), (u1, v1), (u0, v1)], **kw)

    def predict_objects_in_region(self, state: ImageState, polygon, **kw) -> MaskResult:
        """Every object inside a lasso polygon (normalised u, v points): their union."""
        found = self._region_objects(state, polygon, **kw)
        if found is None:
            return MaskResult(None, 0.0, 0)
        cand, score, kept, _area = found
        union = mx.max(cand[mx.array(kept)], axis=0)  # soft union: max of logits
        return MaskResult(np.array(union), float(score[kept[0]]), len(kept))

    def predict_main_in_region(self, state: ImageState, polygon, **kw) -> MaskResult:
        """The dominant object inside a lasso: the largest good object that fits in it."""
        found = self._region_objects(state, polygon, **kw)
        if found is None:
            return MaskResult(None, 0.0, 0)
        cand, score, kept, area = found
        main = max(kept, key=lambda i: (area[i], score[i]))
        return MaskResult(np.array(cand[main]), float(score[main]), 1)

    def _region_objects(
        self,
        state: ImageState,
        polygon,
        min_iou: float = 0.75,
        min_stability: float = 0.85,
        min_inside: float = 0.8,
        chunk: int = 32,
    ):
        """Candidate objects that lie (mostly) inside a lasso.

        Automatic-mask-generator style: single-point prompts on a grid inside
        the lasso (3 masks each) plus a box prompt on the lasso's bounds, all
        held to the same quality bar (predicted IoU, stability) and required to
        lie mostly inside the lasso - anything spilling out is background. A
        loose lasso's box prompt usually fails that bar, which is intended: box
        prompts are only reliable for tight boxes.

        Returns ``(candidates, scores, kept, areas)`` with ``kept`` the
        de-duplicated indices in descending score order, or None.
        """
        R = LOW_RES
        region_np = region_mask(polygon, R)
        if not region_np.any():
            return None
        region = mx.array(region_np.astype(np.float32))
        u0, v0, u1, v1 = _bounds(polygon)

        # Point grid over the lasso's bounds (4-12 per side, by size), keeping
        # only points inside the lasso; refine the grid for thin lassos.
        side_u, side_v = max(u1 - u0, 1e-6), max(v1 - v0, 1e-6)
        n_u = int(np.clip(round(side_u * 16), 4, 12))
        n_v = int(np.clip(round(side_v * 16), 4, 12))
        grid = np.zeros((0, 2), np.float32)
        for _ in range(3):
            us = u0 + (np.arange(n_u) + 0.5) / n_u * side_u
            vs = v0 + (np.arange(n_v) + 0.5) / n_v * side_v
            cand_pts = np.array([(u, v) for v in vs for u in us], np.float32)
            ix = np.clip((cand_pts * R).astype(int), 0, R - 1)
            grid = cand_pts[region_np[ix[:, 1], ix[:, 0]]]
            if len(grid) >= 6:
                break
            n_u, n_v = n_u * 2, n_v * 2

        def keep(masks, iou):
            """Filter (n, R, R) logits on the GPU; returns kept masks and their scores."""
            fg = masks > 0
            area = fg.sum(axis=(-2, -1))
            inside = (fg * region).sum(axis=(-2, -1)) / mx.maximum(area, 1)
            stability = (masks > 1.0).sum(axis=(-2, -1)) / mx.maximum((masks > -1.0).sum(axis=(-2, -1)), 1)
            ok = (iou >= min_iou) & (stability >= min_stability) & (area >= 16) & (inside >= min_inside) & (area <= 0.6 * R * R)
            mx.eval(ok, iou, stability)
            idx = np.flatnonzero(np.array(ok))
            if not len(idx):
                return None, None
            return masks[mx.array(idx)], np.array(iou)[idx] * np.array(stability)[idx]

        pools, scores = [], []
        best = self.predict_box(state, (u0, v0, u1, v1))
        masks, sc = keep(mx.array(best.logits)[None], mx.array([best.score], dtype=mx.float32))
        if masks is not None:
            pools.append(masks)
            scores.append(sc + 1.0)  # a good box-prompt mask wins ties
        for i in range(0, len(grid), chunk):
            g = grid[i : i + chunk] * IMAGE_SIZE
            pts = np.concatenate([g[:, None, :], np.ones((len(g), 1, 1), np.float32)], axis=-1)
            masks, iou = self._decode(state, self._embed_prompts(points=pts), multimask=True)
            masks, sc = keep(masks.astype(mx.float32).reshape(-1, R, R), iou.reshape(-1))
            if masks is not None:
                pools.append(masks)
                scores.append(sc)
        if not pools:
            return None
        cand = mx.concatenate(pools)
        score = np.concatenate(scores)
        # Greedy mask-NMS on foreground IoU (pairwise IoU via one matmul).
        fg = (cand > 0).reshape(cand.shape[0], -1).astype(mx.float16)
        inter = fg @ fg.T
        area = fg.sum(axis=1).astype(mx.float32)
        iou_mat = inter.astype(mx.float32) / mx.maximum(area[:, None] + area[None, :] - inter.astype(mx.float32), 1)
        iou_np, area_np = np.array(iou_mat), np.array(area)
        kept: list[int] = []
        for i in np.argsort(-score):
            if all(iou_np[i, k] <= 0.7 for k in kept):
                kept.append(int(i))
        return cand, score, kept, area_np

    # ------------------------------------------------------------------- text

    def _detector_features(self, state: ImageState):
        if state.detector is None:
            det = self.model.detector_model
            fpn = det.vision_encoder.neck(state.backbone)
            pos = [det._pos_enc(f) for f in fpn]
            levels = fpn[:-1]
            enc = levels[-1]
            B, H, W, D = enc.shape
            src = enc.reshape(B, H * W, D)
            pos_flat = pos[:-1][-1].reshape(B, H * W, D)
            mx.eval(src, pos_flat, levels)
            state.detector = (src, pos_flat, list(levels), (H, W))
        return state.detector

    def _text_embedding(self, text: str):
        if text not in self.text_embeds:
            tok = self.tokenizer([text], padding="max_length", max_length=TEXT_LEN, truncation=True, return_tensors="np")
            ids = mx.array(tok["input_ids"])
            mask = mx.array(tok["attention_mask"])
            emb, mask = self.model.get_input_embeddings(ids, mask)
            mx.eval(emb)
            self.text_embeds[text] = (emb, mask)
            while len(self.text_embeds) > 64:
                self.text_embeds.popitem(last=False)
        self.text_embeds.move_to_end(text)
        return self.text_embeds[text]

    def predict_text(self, state: ImageState, text: str, threshold: float = 0.5, box=None) -> MaskResult:
        """All instances of a concept. Optional box (normalised xyxy) keeps only instances inside it."""
        det = self.model.detector_model
        src, pos, levels, (H, W) = self._detector_features(state)
        emb, mask = self._text_embedding(text.strip())
        encoded = det.detr_encoder(src, pos, emb, mask)
        hs, ref_boxes, presence = det.detr_decoder(
            vision_features=encoded, inputs_embeds=emb, vision_pos_encoding=pos, text_mask=mask, spatial_shape=(H, W)
        )
        logits = det.dot_product_scoring(hs, emb, mask)[-1].squeeze(-1)
        scores = mx.sigmoid(logits[0].astype(mx.float32))
        if presence is not None:
            scores = scores * mx.sigmoid(presence[-1][0].astype(mx.float32))
        seg = det.mask_decoder(hs[-1], levels, encoder_hidden_states=encoded, prompt_features=emb, prompt_mask=mask)
        boxes = ref_boxes[-1][0]  # (Q, 4) cxcywh normalised
        mx.eval(scores, seg["pred_masks"], boxes)
        scores_np = np.array(scores)
        keep = np.nonzero(scores_np > threshold)[0]
        if box is not None and len(keep):
            b = np.array(boxes.astype(mx.float32))[keep]
            cx, cy = b[:, 0], b[:, 1]
            u0, v0, u1, v1 = box
            keep = keep[(cx >= u0) & (cx <= u1) & (cy >= v0) & (cy <= v1)]
        if not len(keep):
            return MaskResult(None, float(scores_np.max(initial=0.0)), 0)
        masks = np.array(seg["pred_masks"][0].astype(mx.float32))[keep]
        if masks.shape[-1] != LOW_RES:
            masks = np.array(_resize_bilinear(mx.array(masks), LOW_RES, LOW_RES))
        return MaskResult(np.maximum.reduce(list(masks)), float(scores_np[keep].max()), len(keep))


# ------------------------------------------------------------------ regions


def _bounds(polygon):
    pts = np.asarray(polygon, np.float32).reshape(-1, 2)
    lo, hi = np.clip(pts.min(axis=0), 0, 1), np.clip(pts.max(axis=0), 0, 1)
    return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


def region_mask(polygon, size: int = LOW_RES) -> np.ndarray:
    """Rasterise a closed polygon of normalised (u, v) points to a (size, size) bool mask."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (size, size), 0)
    pts = [(float(u) * size, float(v) * size) for u, v in np.asarray(polygon, np.float32).reshape(-1, 2)]
    if len(pts) >= 3:
        ImageDraw.Draw(img).polygon(pts, fill=1, outline=1)
    return np.asarray(img, dtype=bool)


# ----------------------------------------------------------------- rendering


def _interp_matrix(src: int, dst_total: int, start: int, count: int) -> mx.array:
    """(count, src) bilinear weights, align_corners=False, edge-clamped."""
    pos = (mx.arange(start, start + count, dtype=mx.float32) + 0.5) * (src / dst_total) - 0.5
    pos = mx.clip(pos, 0, src - 1)
    i0 = mx.floor(pos)
    frac = (pos - i0)[:, None]
    i0 = i0.astype(mx.int32)[:, None]
    i1 = mx.minimum(i0 + 1, src - 1)
    cols = mx.arange(src, dtype=mx.int32)[None, :]
    return (cols == i0) * (1 - frac) + (cols == i1) * frac


def _resize_bilinear(masks: mx.array, h: int, w: int) -> mx.array:
    ry = _interp_matrix(masks.shape[-2], h, 0, h)
    rx = _interp_matrix(masks.shape[-1], w, 0, w)
    return ry @ masks @ rx.T


def mask_bbox(logits: np.ndarray, pad: int = 2):
    """Foreground bbox in low-res pixels (x0, y0, x1, y1), exclusive; None if empty."""
    ys, xs = np.nonzero(logits > -2.0)
    if not len(xs):
        return None
    h, w = logits.shape
    return max(xs.min() - pad, 0), max(ys.min() - pad, 0), min(xs.max() + 1 + pad, w), min(ys.max() + 1 + pad, h)


def render_mask(logits: np.ndarray, out_w: int, out_h: int, antialias: bool = True):
    """Upsample low-res logits to an ``out_w`` x ``out_h`` 8-bit mask, cropped to its bbox.

    Returns ``(x, y, w, h, bytes)`` or ``None`` when empty. Edges are
    anti-aliased with a first-order signed-distance estimate
    (``logit / |grad logit|``), giving ~1 output pixel of coverage ramp at any
    zoom; without anti-aliasing the mask is thresholded at 0 like SAM.
    """
    bb = mask_bbox(logits)
    if bb is None:
        return None
    lh, lw = logits.shape
    x0 = int(math.floor(bb[0] * out_w / lw))
    y0 = int(math.floor(bb[1] * out_h / lh))
    x1 = int(math.ceil(bb[2] * out_w / lw))
    y1 = int(math.ceil(bb[3] * out_h / lh))
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return None
    L = mx.array(logits.astype(np.float32))
    ry = _interp_matrix(lh, out_h, y0, h)
    rx = _interp_matrix(lw, out_w, x0, w)
    up = ry @ L @ rx.T  # (h, w)
    if antialias:
        gx = mx.concatenate([up[:, 1:] - up[:, :-1], mx.zeros((h, 1))], axis=1)
        gy = mx.concatenate([up[1:] - up[:-1], mx.zeros((1, w))], axis=0)
        grad = mx.sqrt(gx * gx + gy * gy)
        alpha = mx.clip(0.5 + up / mx.maximum(grad, 1e-6), 0.0, 1.0)
        # Flat regions far from an edge have ~0 gradient; fall back to the sign.
        alpha = mx.where(grad < 1e-6, (up > 0).astype(mx.float32), alpha)
        out = (alpha * 255.0 + 0.5).astype(mx.uint8)
    else:
        out = (up > 0).astype(mx.uint8) * 255
    # Tighten the crop to the non-zero pixels with GPU reductions (a full-size
    # np.nonzero costs ~150 ms at 8K).
    nz = out > 0
    rows, cols = mx.any(nz, axis=1), mx.any(nz, axis=0)
    mx.eval(out, rows, cols)
    rows, cols = np.flatnonzero(np.array(rows)), np.flatnonzero(np.array(cols))
    if not len(rows):
        return None
    cy0, cy1, cx0, cx1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    data = np.array(out[cy0:cy1, cx0:cx1])
    return x0 + cx0, y0 + cy0, cx1 - cx0, cy1 - cy0, data.tobytes()
