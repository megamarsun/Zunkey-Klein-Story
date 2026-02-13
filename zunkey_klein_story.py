# Zunkey_Klein_Story
# FLUX.2 klein 4B / Tkinter GUI
# - 1行=1プロンプト + 共通プロンプト
# - 参照画像(0-8) + 各スロットにキャラ名(別名OK)を登録して、プロンプト内の名前を imageN に自動置換
# - 出力プレビューは ←→ ボタン + キーボード ←→ で移動
# - 修正ボタン: プレビュー中の画像を image1 として再投入し、破綻修正を狙う（編集モード）
#
# NOTE:
# - klein のマルチ参照は環境によって引数名が違うので、実行時に signature を見て自動分岐します。
# - 修正ボタンでは、現在の参照スロットから最大3枚までを追加し、合計4枚（image1=ベース + image2-4=参照）に制限します。
#
# v22: UI簡素化 + Inpaint品質改善 (v21ベース, ロジック全て維持)
# - メニューバーに Settings/Presets/Export を集約
# - 左パネルを Notebook タブ化 (Prompt / Config / Refs)
# - Config タブにスクロール対応
# - 右パネル: プレビュー可変サイズ化, ナビ行コンパクト化
# - プロンプト表示を折りたたみ式に (Prompt チェックボックス)
# - マークボタン = G/S/B/U 小ボタン (キーボードがメイン)
# - パディング全面削減, ログ高さ削減
#
# Inpaint (v22 hybrid: direct damage + mask composite):
# - 画像に直接ペイント(白/赤で塗りつぶし)→ Kleinに渡す (Kleinが「破損」を認識)
# - Klein出力後、差分から自動マスクを生成 → grow+blur → ソフトマスク合成
# - 破損領域だけKlein出力を使用、他は元画像を完全保持 → 過学習的劣化を防止
# - damage_blur: ペイント境界をぼかしてKleinに渡す → 自然な修復を促す
# - Inpaintタブ: Before/After プレビュー + 設定 + 実行ボタン
#
# NanoBanana Pro (Gemini Image API) タブ:
# - Refine: Klein生成物をGemini APIに渡して高品質化
# - Edit: 画像+指示テキストで編集
# - Generate: テキストのみで新規生成
# - モデル選択: gemini-3-pro-image-preview (高品質) / gemini-2.5-flash-image (高速)
# - 解像度: 1K/2K/4K、アスペクト比選択
# - Quick presets: 高品質化/アニメ清書/実写化/背景修正
# - 追加リファレンス画像対応

import os
import sys
import json
import re
import time
import threading
import subprocess
import inspect
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk, ImageDraw, ImageOps, ImageFilter


APP_NAME = 'Zunkey_Klein_Story'

# Preview sizing
PREVIEW_MAX = 900  # used by damage painter popup
DEFAULT_REPO_ID = 'black-forest-labs/FLUX.2-klein-4B'
MAX_REFS = 8

PRESETS_DIRNAME = 'settings_presets'
REPAIR_MAX_TOTAL_IMAGES = 4  # base + refs (klein想定)
SETTINGS_FILENAME = "zunkey_klein_story_settings.json"


# ---------------------------------------------------------------------------
# Tooltip helper (Tkinter has no built-in tooltip)
# ---------------------------------------------------------------------------

class ToolTip:
    """Hover tooltip for any Tkinter widget."""
    def __init__(self, widget, text: str, delay: int = 400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip = None
        self._after_id = None
        widget.bind('<Enter>', self._on_enter, add='+')
        widget.bind('<Leave>', self._on_leave, add='+')
        widget.bind('<ButtonPress>', self._on_leave, add='+')

    def _on_enter(self, event=None):
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        self._hide()

    def _show(self):
        if self._tip:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f'+{x}+{y}')
        lbl = tk.Label(tw, text=self.text, justify='left',
                       background='#ffffdd', foreground='#333',
                       relief='solid', borderwidth=1,
                       font=('', 9), padx=6, pady=3, wraplength=360)
        lbl.pack()

    def _hide(self):
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def update_text(self, text: str):
        self.text = text


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _now_stamp() -> str:
    return time.strftime('%Y%m%d_%H%M%S')


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return None


def _is_text_focus_widget(w: Optional[tk.Widget]) -> bool:
    if w is None:
        return False
    cls = w.winfo_class()
    return cls in ('Entry', 'Text', 'TEntry', 'TCombobox', 'Spinbox')


def _round_down_16(x: int) -> int:
    if x <= 0:
        return x
    return max(16, (x // 16) * 16)


def _fit_within_2048(w: int, h: int) -> Tuple[int, int]:
    """Scale so the longer side becomes 2048px (keeps aspect ratio).
    Output is rounded down to multiples of 16 (FLUX likes 16x)."""
    if w <= 0 or h <= 0:
        return w, h
    scale = 2048.0 / float(max(w, h))
    nw = int(w * scale)
    nh = int(h * scale)
    nw = max(16, min(2048, nw))
    nh = max(16, min(2048, nh))
    nw = _round_down_16(nw)
    nh = _round_down_16(nh)
    return nw, nh


# ---------------------------------------------------------------------------
# Dependency installer
# ---------------------------------------------------------------------------

def ensure_python_deps(log_cb=None) -> None:
    required = [
        ('torch', 'torch'),
        ('diffusers', 'diffusers'),
        ('transformers', 'transformers'),
        ('accelerate', 'accelerate'),
        ('safetensors', 'safetensors'),
        ('huggingface_hub', 'huggingface_hub'),
        ('PIL', 'Pillow'),
    ]
    missing = []
    for mod, pipname in required:
        try:
            __import__(mod)
        except Exception:
            missing.append(pipname)

    if not missing:
        return

    if log_cb:
        log_cb('Installing python deps: ' + ', '.join(missing))

    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade'] + missing
        subprocess.check_call(cmd)
        if log_cb:
            log_cb('Deps install done.')
    except Exception as e:
        if log_cb:
            log_cb(f'Deps install failed: {e}')


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@dataclass
class AppPaths:
    script_dir: Path
    tmp_dir: Path
    models_dir: Path
    outputs_dir: Path
    hf_home: Path
    hf_cache: Path


def load_paths() -> AppPaths:
    script_dir = Path(__file__).resolve().parent

    tmp_dir: Optional[Path] = None
    env_tmp = os.environ.get('FLUX_TK_TMP', '').strip()
    if env_tmp:
        tmp_dir = Path(env_tmp)

    if tmp_dir is None:
        cfg = _read_json(script_dir / 'app_config.json') or {}
        cfg_tmp = (cfg.get('tmp_dir') or '').strip()
        if cfg_tmp:
            tmp_dir = Path(cfg_tmp)

    if tmp_dir is None:
        tmp_dir = script_dir / 'TMP'

    models_dir = tmp_dir / 'models'
    outputs_dir = tmp_dir / 'outputs'
    hf_home = tmp_dir / 'hf_home'
    hf_cache = hf_home / 'hub'

    _safe_mkdir(models_dir)
    _safe_mkdir(outputs_dir)
    _safe_mkdir(hf_cache)

    os.environ.setdefault('HF_HOME', str(hf_home))
    os.environ.setdefault('HUGGINGFACE_HUB_CACHE', str(hf_cache))
    os.environ.setdefault('TRANSFORMERS_CACHE', str(hf_cache))

    return AppPaths(
        script_dir=script_dir,
        tmp_dir=tmp_dir,
        models_dir=models_dir,
        outputs_dir=outputs_dir,
        hf_home=hf_home,
        hf_cache=hf_cache,
    )


# ---------------------------------------------------------------------------
# Model detection / auto-install
# ---------------------------------------------------------------------------

def detect_4b_model_dir(models_dir: Path) -> Optional[Path]:
    if not models_dir.exists():
        return None
    dirs = [p for p in models_dir.iterdir() if p.is_dir()]
    if not dirs:
        return None

    def score(p: Path) -> int:
        name = p.name.lower()
        s = 0
        if 'klein' in name:
            s += 10
        if '4b' in name:
            s += 10
        if 'flux' in name:
            s += 3
        return s

    dirs_sorted = sorted(dirs, key=lambda p: (score(p), p.stat().st_mtime), reverse=True)
    if len(dirs_sorted) == 1:
        return dirs_sorted[0]
    if score(dirs_sorted[0]) >= 10:
        return dirs_sorted[0]
    return None


def auto_install_4b(models_dir: Path, log_cb=None) -> Path:
    existing = detect_4b_model_dir(models_dir)
    if existing:
        return existing

    target = models_dir / 'FLUX.2-klein-4B'
    if target.exists():
        try:
            if any(target.iterdir()):
                return target
        except Exception:
            pass

    if log_cb:
        log_cb('4B model not found. Auto downloading...')

    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=DEFAULT_REPO_ID,
        local_dir=str(target),
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    if log_cb:
        log_cb(f'Download done: {target}')
    return target


# ---------------------------------------------------------------------------
# FLUX Klein Engine
# ---------------------------------------------------------------------------

class FluxKleinEngine:
    def __init__(self, log_cb=None):
        self.log_cb = log_cb
        self.pipe = None
        self.pipe_name = '-'
        self.model_path = None
        self.device = 'cuda' if self._has_cuda() else 'cpu'

    @staticmethod
    def _has_cuda() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except Exception:
            return False

    def load(self, model_path: Path, cpu_offload: bool = False) -> None:
        ensure_python_deps(self.log_cb)

        import torch
        from diffusers import DiffusionPipeline

        self.model_path = str(model_path)

        pipe = None
        pipe_name = None

        try:
            from diffusers import Flux2KleinPipeline
            pipe = Flux2KleinPipeline.from_pretrained(
                self.model_path,
                torch_dtype=torch.bfloat16 if self.device == 'cuda' else torch.float32,
            )
            pipe_name = 'Flux2KleinPipeline'
        except Exception:
            pipe = None

        if pipe is None:
            try:
                from diffusers import FluxPipeline
                pipe = FluxPipeline.from_pretrained(
                    self.model_path,
                    torch_dtype=torch.bfloat16 if self.device == 'cuda' else torch.float32,
                )
                pipe_name = 'FluxPipeline'
            except Exception:
                pipe = None

        if pipe is None:
            pipe = DiffusionPipeline.from_pretrained(
                self.model_path,
                torch_dtype=torch.bfloat16 if self.device == 'cuda' else torch.float32,
            )
            pipe_name = type(pipe).__name__

        if self.device == 'cuda':
            try:
                pipe.to('cuda')
            except Exception as e:
                if self.log_cb:
                    self.log_cb(f'[WARN] pipe.to(cuda) failed: {e}')

        if cpu_offload:
            for fn in ('enable_model_cpu_offload', 'enable_sequential_cpu_offload'):
                if hasattr(pipe, fn):
                    try:
                        getattr(pipe, fn)()
                        break
                    except Exception as e:
                        if self.log_cb:
                            self.log_cb(f'[WARN] {fn} failed: {e}')

        self.pipe = pipe
        self.pipe_name = pipe_name or type(pipe).__name__
        if self.log_cb:
            self.log_cb(f'Loaded model: {self.model_path}')
            self.log_cb(f'pipe: {self.pipe_name}  device: {self.device}')

    def _call_signature(self):
        if self.pipe is None:
            return None
        try:
            return inspect.signature(self.pipe.__call__)
        except Exception:
            return None

    def supports_inpaint(self) -> bool:
        sig = self._call_signature()
        params = set(sig.parameters.keys()) if sig else set()
        return ('mask_image' in params) or ('mask' in params)

    def _build_kwargs(
        self,
        prompt: str,
        width: int,
        height: int,
        steps: int,
        guidance: float,
        seed: int,
        ref_images: Optional[List[Image.Image]],
        mask_image: Optional[Image.Image] = None,
        strength: Optional[float] = None,
    ) -> Tuple[dict, str, int]:
        sig = self._call_signature()
        params = set(sig.parameters.keys()) if sig else set()

        import torch

        kwargs = {}
        if 'prompt' in params or not params:
            kwargs['prompt'] = prompt

        if 'num_inference_steps' in params:
            kwargs['num_inference_steps'] = steps
        elif 'steps' in params:
            kwargs['steps'] = steps
        elif 'num_steps' in params:
            kwargs['num_steps'] = steps

        if 'guidance_scale' in params:
            kwargs['guidance_scale'] = guidance
        elif 'guidance' in params:
            kwargs['guidance'] = guidance
        elif 'cfg' in params:
            kwargs['cfg'] = guidance

        if 'width' in params:
            kwargs['width'] = width
        if 'height' in params:
            kwargs['height'] = height

        used_seed = seed
        if used_seed < 0:
            used_seed = int.from_bytes(os.urandom(4), 'little') & 0x7FFFFFFF

        if 'generator' in params:
            try:
                kwargs['generator'] = torch.Generator(device=self.device).manual_seed(used_seed)
            except Exception as e:
                if self.log_cb:
                    self.log_cb(f'[WARN] Generator creation failed: {e}')

        if 'output_type' in params:
            kwargs['output_type'] = 'pil'

        ref_desc = 'none'
        if ref_images:
            imgs = [im.convert('RGB') for im in ref_images if im is not None]
            if imgs:
                if 'images' in params:
                    kwargs['images'] = imgs
                    ref_desc = 'images(list)'
                elif 'reference_images' in params:
                    kwargs['reference_images'] = imgs
                    ref_desc = 'reference_images(list)'
                elif 'input_images' in params:
                    kwargs['input_images'] = imgs
                    ref_desc = 'input_images(list)'
                elif 'image' in params:
                    kwargs['image'] = imgs if len(imgs) > 1 else imgs[0]
                    ref_desc = 'image(list)' if len(imgs) > 1 else 'image'
                elif 'input_image' in params:
                    kwargs['input_image'] = imgs[0]
                    ref_desc = 'input_image'
                else:
                    ref_desc = 'ignored(no_param)'

        # strength (img2img / inpaint partial denoise)
        if strength is not None and 'strength' in params:
            kwargs['strength'] = float(strength)

        mask_desc = None
        if mask_image is not None:
            try:
                m = mask_image.convert('L')
                if 'mask_image' in params:
                    kwargs['mask_image'] = m
                    mask_desc = 'mask_image'
                elif 'mask' in params:
                    kwargs['mask'] = m
                    mask_desc = 'mask'
            except Exception as e:
                if self.log_cb:
                    self.log_cb(f'[WARN] mask conversion failed: {e}')

        if mask_desc:
            if ref_desc == 'none':
                ref_desc = mask_desc
            else:
                ref_desc = f'{ref_desc}+{mask_desc}'
        return kwargs, ref_desc, used_seed

    def generate_one(
        self,
        prompt: str,
        width: int,
        height: int,
        steps: int,
        guidance: float,
        seed: int,
        ref_images: Optional[List[Image.Image]],
        mask_image: Optional[Image.Image] = None,
        strength: Optional[float] = None,
    ) -> Tuple[Image.Image, int, str, float]:
        if self.pipe is None:
            raise RuntimeError('Model not loaded')

        t0 = time.time()
        kwargs, ref_desc, used_seed = self._build_kwargs(prompt, width, height, steps, guidance, seed, ref_images, mask_image, strength)

        try:
            out = self.pipe(**kwargs)
        except TypeError as e:
            if 'image' in kwargs and isinstance(kwargs['image'], list) and kwargs['image']:
                kwargs2 = dict(kwargs)
                kwargs2['image'] = kwargs['image'][0]
                ref_desc = 'image'
                out = self.pipe(**kwargs2)
            else:
                raise e

        img = None
        if hasattr(out, 'images'):
            if out.images:
                img = out.images[0]
        if img is None and isinstance(out, (list, tuple)) and out:
            img = out[0]
        if img is None:
            raise RuntimeError('No image returned from pipeline')

        dt = time.time() - t0
        return img, used_seed, ref_desc, dt


# ---------------------------------------------------------------------------
# Prompt Resolver
# ---------------------------------------------------------------------------

class PromptResolver:
    @staticmethod
    def parse_used_image_indices(text: str) -> List[int]:
        pattern = r'\[?\s*image\s*([1-' + str(MAX_REFS) + r'])\s*\]?'
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        idxs: List[int] = []
        for x in found:
            try:
                i = int(x)
                if 1 <= i <= MAX_REFS and i not in idxs:
                    idxs.append(i)
            except Exception:
                pass
        return idxs

    @staticmethod
    def build_alias_map(ref_names: List[str]) -> List[Tuple[str, int]]:
        pairs: List[Tuple[str, int]] = []
        for i, raw in enumerate(ref_names, start=1):
            s = (raw or '').strip()
            if not s:
                continue
            parts = re.split(r'[,、;|]+', s)
            for a in parts:
                a = a.strip()
                if not a:
                    continue
                pairs.append((a, i))
        pairs.sort(key=lambda t: len(t[0]), reverse=True)
        return pairs

    @staticmethod
    def substitute_names(text: str, alias_pairs: List[Tuple[str, int]]) -> str:
        out = text
        for alias, idx in alias_pairs:
            out = out.replace(alias, f'image{idx}')
        return out

    @staticmethod
    def brackify_images(text: str) -> str:
        return re.sub(r'(?i)\bimage\s*([1-8])\b', r'[image\1]', text)

    @staticmethod
    def shift_image_indices(text: str, delta: int) -> str:
        if delta == 0:
            return text

        def repl(m):
            n = int(m.group(1))
            nn = n + delta
            if nn < 1:
                nn = 1
            return f'image{nn}'

        return re.sub(r'(?i)\bimage\s*([1-8])\b', repl, text)


# ---------------------------------------------------------------------------
# Output Browser
# ---------------------------------------------------------------------------

class OutputBrowser:
    def __init__(self, outputs_dir: Path):
        self.outputs_dir = outputs_dir
        self.files: List[Path] = []
        self.index = -1

    def rescan(self) -> None:
        if not self.outputs_dir.exists():
            self.files = []
            self.index = -1
            return
        pngs = sorted(self.outputs_dir.glob('*.png'), key=lambda p: p.stat().st_mtime)
        self.files = pngs
        self.index = len(pngs) - 1 if pngs else -1

    def current(self) -> Optional[Path]:
        if 0 <= self.index < len(self.files):
            return self.files[self.index]
        return None

    def prev(self) -> Optional[Path]:
        if not self.files:
            return None
        self.index = max(0, self.index - 1)
        return self.current()

    def next(self) -> Optional[Path]:
        if not self.files:
            return None
        self.index = min(len(self.files) - 1, self.index + 1)
        return self.current()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class ZunkeyKleinStoryApp(tk.Tk):

    # ==================== P4: Declarative settings map ====================
    # (json_key, tk_var_attr, type_fn, default_value)
    _SETTINGS_MAP = [
        ('model_path',       'model_path_var',       str,   ''),
        ('prefix',           'prefix_var',           str,   'story'),
        ('width',            'w_var',                int,   1024),
        ('height',           'h_var',                int,   1024),
        ('steps',            'steps_var',            int,   18),
        ('guidance',         'guidance_var',         float, 0.7),
        ('cpu_offload',      'cpu_offload_var',      bool,  False),
        ('random_seed',      'random_seed_var',      bool,  True),
        ('seed',             'seed_var',             int,   -1),
        ('samples_per_line', 'samples_var',          int,   4),
        ('repair_steps',     'repair_steps_var',     int,   24),
        ('repair_guidance',  'repair_guidance_var',  float, 0.7),
        ('use_original_seed','use_original_seed_var',bool,  True),
        ('fix_skip_confirm', '_fix_skip_confirm_var', bool, False),
        ('repair_prefix_use','repair_prefix_use_var',bool,  False),
        ('inpaint_prefix_use','inpaint_prefix_use_var',bool, False),
        ('repair_prefix',    'repair_prefix_var',    str,   ''),
        ('inpaint_prefix',   'inpaint_prefix_var',   str,   ''),
        ('inpaint_override', 'inpaint_override_var', str,   ''),
        ('inpaint_use_full_ref','inpaint_use_full_ref_var', bool, True),
        ('inpaint_mode',       'inpaint_mode_var',        str,  'full'),
        ('inpaint_pad_pct',  'inpaint_pad_pct_var',  float, 0.15),
        ('inpaint_min_crop', 'inpaint_min_crop_var', int,   512),
        ('inpaint_mask_grow','inpaint_mask_grow_var', int,   4),
        ('inpaint_mask_blur','inpaint_mask_blur_var', int,   8),
        ('inpaint_damage_blur','inpaint_damage_blur_var', int, 0),
        ('common_base',      'common_base_var',      str,   ''),
        ('common_story',     'common_story_var',     str,   ''),
        ('common_strict',    'common_strict_var',    str,   ''),
        ('common_story_on',  'common_story_on_var',  bool,  False),
        ('common_strict_on', 'common_strict_on_var', bool,  False),
        ('scan_latest_only', 'scan_latest_var',      bool,  True),
        ('scan_latest_n',    'scan_latest_n_var',    int,   800),
        ('preview_zoom_fit', '_preview_zoom_fit',    bool,  False),
        # NanoBanana (Gemini API)
        ('nb_api_key',       'nb_api_key_var',       str,   ''),
        ('nb_model',         'nb_model_var',         str,   'gemini-3-pro-image-preview'),
        ('nb_resolution',    'nb_resolution_var',    str,   '2K'),
        ('nb_aspect',        'nb_aspect_var',        str,   'original'),
        ('nb_mode',          'nb_mode_var',          str,   'refine'),
        ('nb_refine_prompt', 'nb_refine_prompt_var', str,
         'Redraw this image with correct human anatomy. '
         'Fix all errors: wrong number of fingers, broken hands, '
         'distorted faces, extra limbs, merged body parts. '
         'Every person must have exactly 5 fingers on each hand. '
         'Keep the same composition, characters, style, and colors.'),
        ('nb_edit_prompt',   'nb_edit_prompt_var',   str,   ''),
        ('nb_gen_prompt',    'nb_gen_prompt_var',    str,   ''),
    ]

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry('1280x760')

        self.paths = load_paths()
        self.engine = FluxKleinEngine(log_cb=self._log_threadsafe)
        self.browser = OutputBrowser(self.paths.outputs_dir)

        # Sidecar cache (speed up preview browsing)
        self._sidecar_cache: OrderedDict = OrderedDict()
        self._sidecar_cache_max: int = 128
        self._total_png_count: int = 0
        self._prompt_history: List[str] = []  # recent prompts (newest first)
        self._prompt_history_max: int = 50

        # Inpaint mask cache
        self._inpaint_masks: dict = {}
        self._inpaint_mask_paths: dict = {}
        self._inpaint_damaged: dict = {}  # key -> PIL.Image (damaged/painted-over image)

        # Output filter
        self.output_filter_var = tk.StringVar(value='全て')

        # Output scan options
        self.scan_latest_var = tk.BooleanVar(value=True)
        self.scan_latest_n_var = tk.IntVar(value=800)
        self._current_mark = ''

        self.stop_flag = threading.Event()
        self.is_generating = False
        self._progress_dialog = None
        self._prog_anim_running = False
        self.job_counter = 0
        self._loading_settings = False
        self._save_settings_after_id = None

        self.ref_paths: List[Optional[str]] = [None] * MAX_REFS
        self.ref_name_vars: List[tk.StringVar] = [tk.StringVar(value='') for _ in range(MAX_REFS)]
        self.ref_file_labels: List[ttk.Label] = []

        # Settings file path
        self.settings_path_var = tk.StringVar(value=str(self.paths.script_dir / SETTINGS_FILENAME))
        try:
            lp = self.paths.script_dir / 'zunkey_klein_story_settings_last.txt'
            if lp.exists():
                s = lp.read_text(encoding='utf-8', errors='ignore').strip()
                if s:
                    p = Path(s)
                    if p.exists():
                        self.settings_path_var.set(str(p))
        except Exception:
            pass

        # Presets
        self.presets_dir = self.paths.script_dir / PRESETS_DIRNAME
        _safe_mkdir(self.presets_dir)
        self.preset_select_var = tk.StringVar(value='')
        self.preset_name_var = tk.StringVar(value='')

        self._build_ui()

        # Settings
        self._load_settings(silent=True)
        self._refresh_presets()
        self._bind_autosave_traces()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

        # P2: Key bindings via dictionary dispatch
        self._last_key_ts: dict = {}
        self._setup_keybinds()

        self.after(200, self._auto_bootstrap)

    # ==================== Logging ====================

    def log(self, msg: str) -> None:
        """Log to the GUI log widget. Must be called from the main thread."""
        try:
            self.log_text.configure(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        except Exception:
            pass
        # Also update progress dialog if open
        try:
            if self._progress_dialog and self._progress_dialog.winfo_exists():
                self._prog_log.configure(state='normal')
                self._prog_log.insert('end', msg + '\n')
                self._prog_log.see('end')
                self._prog_log.configure(state='disabled')
                self._prog_status_var.set(msg[:120])
        except Exception:
            pass

    def _log_threadsafe(self, msg: str) -> None:
        """Log from any thread — delegates to main thread via after()."""
        try:
            self.after(0, lambda m=msg: self.log(m))
        except Exception:
            pass

    # --- Modal progress dialog ---

    def _open_progress(self, title: str = 'Processing...') -> None:
        """Open modal progress dialog that blocks main window interaction."""
        if self._progress_dialog is not None:
            try:
                self._progress_dialog.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry('460x320')
        dlg.transient(self)
        dlg.resizable(True, True)

        # Status
        self._prog_status_var = tk.StringVar(value='Starting...')
        ttk.Label(dlg, textvariable=self._prog_status_var,
                  wraplength=440, font=('', 10)).pack(padx=8, pady=(8, 4), anchor='w')

        # Progress bar
        self._prog_bar_var = tk.DoubleVar(value=0)
        self._prog_bar = ttk.Progressbar(dlg, variable=self._prog_bar_var,
                                          maximum=100, mode='determinate')
        self._prog_bar.pack(fill='x', padx=8, pady=(0, 2))
        self._prog_pct_var = tk.StringVar(value='')
        ttk.Label(dlg, textvariable=self._prog_pct_var, font=('', 9)).pack(padx=8, anchor='e')

        # Thumbnail preview of latest generated image
        self._prog_thumb_canvas = tk.Canvas(dlg, height=80, bg='#2a2a2a', highlightthickness=0)
        self._prog_thumb_canvas.pack(fill='x', padx=8, pady=2)
        self._prog_thumb_img = None  # prevent GC

        # Mini log
        self._prog_log = tk.Text(dlg, height=6, state='disabled', wrap='word')
        self._prog_log.pack(fill='both', expand=True, padx=8, pady=2)

        # Stop button
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill='x', padx=8, pady=(4, 8))
        ttk.Button(btn_frame, text='■ 停止', command=self._stop_clicked).pack(side='left')
        ttk.Label(btn_frame, text='メインウィンドウは生成完了まで操作できません',
                  font=('', 8), foreground='gray').pack(side='right')

        self._progress_dialog = dlg

        # Prevent closing via X button (must use Stop)
        dlg.protocol('WM_DELETE_WINDOW', self._stop_clicked)

        # Grab AFTER all widgets are created
        dlg.update_idletasks()
        dlg.grab_set()

        # Start animation
        self._prog_anim_running = True
        self._animate_progress()

    def _animate_progress(self) -> None:
        """Animate the progress bar when in indeterminate mode."""
        if not self._prog_anim_running:
            return
        try:
            if self._progress_dialog and self._progress_dialog.winfo_exists():
                # Pulse the progress bar title with dots
                if hasattr(self, '_prog_pct_var'):
                    cur = self._prog_pct_var.get()
                    if not cur or cur.startswith('処理中'):
                        dots = cur.count('.') if cur else 0
                        self._prog_pct_var.set('処理中' + '.' * ((dots % 3) + 1))
                self.after(500, self._animate_progress)
        except Exception:
            pass

    def _close_progress(self) -> None:
        """Close modal progress dialog."""
        self._prog_anim_running = False
        try:
            if self._progress_dialog and self._progress_dialog.winfo_exists():
                self._progress_dialog.grab_release()
                self._progress_dialog.destroy()
        except Exception:
            pass
        self._progress_dialog = None
        self._prog_thumb_img = None

    def _update_progress(self, done: int, total: int, msg: str = '') -> None:
        """Update progress bar value and percentage text."""
        try:
            if total > 0:
                pct = done / total * 100
                self._prog_bar_var.set(pct)
                self._prog_pct_var.set(f'{done}/{total} ({pct:.0f}%)  {msg}')
            else:
                self._prog_pct_var.set(msg)
        except Exception:
            pass

    def _update_progress_thumb(self, img: 'Image.Image') -> None:
        """Show thumbnail of latest generated image in progress dialog."""
        try:
            c = self._prog_thumb_canvas
            if not c or not c.winfo_exists():
                return
            c.delete('all')
            iw, ih = img.size
            c.update_idletasks()
            cw = max(100, c.winfo_width() - 4)
            ch = 80
            scale = min(cw / iw, ch / ih, 1.0)
            tw = max(1, int(iw * scale))
            th = max(1, int(ih * scale))
            thumb = img.resize((tw, th), Image.LANCZOS) if scale < 1.0 else img.copy()
            tkimg = ImageTk.PhotoImage(thumb)
            c.configure(height=th)
            c.create_image(cw // 2, 0, anchor='n', image=tkimg)
            self._prog_thumb_img = tkimg
        except Exception:
            pass

    # ==================== UI Build (compact tab layout) ====================

    def _build_ui(self) -> None:
        self._build_menubar()

        pw = ttk.PanedWindow(self, orient='horizontal')
        pw.pack(fill='both', expand=True)

        left = ttk.Frame(pw)
        right = ttk.Frame(pw)
        pw.add(left, weight=2)
        pw.add(right, weight=3)

        self._active_scroll_canvas = None  # unified mousewheel target
        self._preview_zoom_fit = tk.BooleanVar(value=False)

        self._build_left_tabs(left)
        self._build_right_panel(right)

        # Unified mousewheel handler (dispatches to active canvas)
        def _global_wheel(ev):
            c = self._active_scroll_canvas
            if c is None:
                return
            # Preview canvas: Ctrl+wheel = navigate prev/next image
            if c is self._preview_canvas and (ev.state & 0x4):  # Ctrl key
                up = (ev.delta > 0 if ev.delta else ev.num == 4)
                if up:
                    self._prev_image()
                else:
                    self._next_image()
                return
            # Preview canvas: Shift=horizontal scroll
            if c is self._preview_canvas and ev.state & 0x1:
                delta = -1 if (ev.delta > 0 if ev.delta else ev.num == 4) else 1
                c.xview_scroll(delta, 'units')
                return
            if ev.num == 4:
                c.yview_scroll(-3, 'units')
            elif ev.num == 5:
                c.yview_scroll(3, 'units')
            elif ev.delta:
                c.yview_scroll(-1 * (ev.delta // 120), 'units')
        self.bind_all('<MouseWheel>', _global_wheel)
        self.bind_all('<Button-4>', _global_wheel)
        self.bind_all('<Button-5>', _global_wheel)

        # Tooltips (hover help)
        self._setup_tooltips()

    def _setup_tooltips(self) -> None:
        """全ウィジェットにマウスオーバーのヒントを設定"""
        T = ToolTip  # shorthand

        # ── プロンプトタブ ──
        T(self._gen_btn, 'プロンプト欄の各行から画像を生成します\n（1行＝1枚の画像指示）')
        T(self._stop_btn, '生成を途中で止めます')
        T(self.pipe_label, '現在読み込まれているAIモデル名')

        # ── 設定タブ ──
        try:
            T(self._chk_cpuoff, 'VRAM不足時にON → 遅くなるがGPUメモリ節約')
            T(self._cfg_stp_lbl, '修正時のステップ数（多い＝高品質だが遅い）')
            T(self._cfg_cfg_lbl, '修正時のガイダンス強度（0.5〜1.5 推奨）')
        except AttributeError:
            pass

        # ── 修復タブ（マスク設定）──
        try:
            T(self._mask_grow_lbl, 'マスクを何ピクセル膨張させるか\n（塗った範囲を少し広げる）')
            T(self._mask_blur_lbl, 'マスク境界のぼかし量\n（大きい＝自然な境目）')
            T(self._mask_dmg_lbl, 'ペイント部分の境界ぼかし\n（Kleinに渡す前の前処理）')
        except AttributeError:
            pass

        # ── 右パネル：ナビゲーション ──
        T(self._btn_prev, '前の画像を表示（← キーでも可）')
        T(self._btn_next, '次の画像を表示（→ キーでも可）')
        T(self._btn_rescan, '出力フォルダを再スキャン')
        T(self._chk_latest, 'ONにすると最新N枚だけ表示')
        T(self._spn_latest, '表示する最大枚数')
        T(self.filter_combo, '画像のフィルタ\nAll=全部 / Good=良い / Star=お気に入り / Bad=悪い')
        T(self._btn_fix, 'この画像をベースに破綻を自動修正\n（参照画像と同じスタイルで再生成）')

        # ── マーク ──
        T(self._chk_fit, 'ON: 画面に合わせて縮小表示\nOFF: 100%原寸（スクロール可）\nキー: Z')
        mark_tips = {
            'G': 'Good（良い画像）に印をつける（キー: G）',
            'S': 'Star（お気に入り）に印をつける（キー: S）',
            'B': 'Bad（悪い画像）に印をつける（キー: B）',
            'U': '印を解除する（キー: U）',
        }
        for txt, tip in mark_tips.items():
            if txt in self._mark_btns:
                T(self._mark_btns[txt], tip)

        # ── プロンプト表示ボタン ──
        T(self._btn_cp_raw, '入力したプロンプト（生テキスト）をコピー')
        T(self._btn_cp_res, '名前→imageN に変換後のプロンプトをコピー')
        T(self._btn_cp_fix, '修正用プロンプトを自動生成してコピー')
        T(self._btn_add_raw, '生プロンプトをプロンプト欄の末尾に追加')
        T(self._btn_add_res, '解決済みプロンプトをプロンプト欄の末尾に追加')

        # ── NanoBanana ──
        try:
            T(self._nb_run_btn, 'Gemini APIに画像とプロンプトを送信\n（要APIキー・通信料は自己負担）')
            T(self._nb_save_btn, 'API結果を出力フォルダに保存')
            T(self._nb_rerun_btn, '結果画像を再度NanoBananaに送信\n（繰り返し改善に便利）')
            T(self._nb_ref_btn, 'NB結果を参照画像スロットにセット\n（Klein生成の参照として使用）')
        except AttributeError:
            pass

        # ── スライダー ──
        T(self._nav_slider, 'ドラッグで画像を素早くジャンプ')

        # ── Seed ──
        T(self._used_seed_lbl, 'クリック → seedをコピー＆フィールドにセット\n（同じ画像を再生成したい時に便利）')

        # ── 即修正 ──
        T(self._chk_fix_skip, 'ONにすると修正ボタン/Rキーで確認なし即実行\n（連続修正作業に便利）')

    # ---------- Menu bar (Settings / Presets / Export) ----------

    def _build_menubar(self) -> None:
        mbar = tk.Menu(self)
        self.configure(menu=mbar)

        fm = tk.Menu(mbar, tearoff=0)
        mbar.add_cascade(label='ファイル', menu=fm)
        fm.add_command(label='設定を保存', command=self._save_settings_clicked)
        fm.add_command(label='設定を別名で保存…', command=self._save_settings_as_clicked)
        fm.add_command(label='設定を読み込み…', command=self._load_settings_clicked)
        fm.add_separator()
        fm.add_command(label='出力フォルダを開く', command=self._open_outputs)
        fm.add_separator()
        fm.add_command(label='Good画像をエクスポート → _good/', command=self._export_good_images)
        fm.add_command(label='Star画像をエクスポート → _star/', command=self._export_star_images)

        self._presets_menu = tk.Menu(mbar, tearoff=0)
        mbar.add_cascade(label='プリセット', menu=self._presets_menu)
        self._presets_menu.add_command(label='プリセットを保存…', command=self._save_preset_clicked)
        self._presets_menu.add_command(label='プリセットを削除…', command=self._delete_preset_clicked)
        self._presets_menu.add_separator()

    # ---------- Left panel: Notebook tabs ----------

    def _build_left_tabs(self, parent) -> None:
        nb = ttk.Notebook(parent)
        nb.pack(fill='both', expand=True, padx=2, pady=2)
        self._left_nb = nb

        tab_prompt  = ttk.Frame(nb)
        tab_config  = ttk.Frame(nb)
        tab_refs    = ttk.Frame(nb)
        tab_inpaint = ttk.Frame(nb)
        tab_nb      = ttk.Frame(nb)
        nb.add(tab_prompt,  text=' プロンプト ')
        nb.add(tab_config,  text=' 設定 ')
        nb.add(tab_refs,    text=' 参照画像 ')
        nb.add(tab_inpaint, text=' 修復 ')
        nb.add(tab_nb,      text=' 🍌NanoBanana ')

        self._build_tab_prompt(tab_prompt)
        self._build_tab_config(tab_config)
        self._build_tab_refs(tab_refs)
        self._build_tab_inpaint(tab_inpaint)
        self._build_tab_nanobanana(tab_nb)

    def _build_tab_prompt(self, p) -> None:
        ttk.Label(p, text='1行=1プロンプト（空行は無視）').pack(anchor='w', padx=4, pady=(4, 0))
        self.prompts_text = tk.Text(p, height=10)
        self.prompts_text.pack(fill='both', expand=True, padx=4, pady=2)

        # 共通プロンプト (compact)
        cf = ttk.LabelFrame(p, text='共通 (基本=常時, 物語/厳密=ON時)')
        cf.pack(fill='x', padx=4, pady=(0, 2))

        self.common_base_var   = tk.StringVar(value='')
        self.common_story_var  = tk.StringVar(value='')
        self.common_strict_var = tk.StringVar(value='')
        self.common_story_on_var  = tk.BooleanVar(value=False)
        self.common_strict_on_var = tk.BooleanVar(value=False)

        for lbl, var, chk_var in [
            ('基本',  self.common_base_var,   None),
            ('物語', self.common_story_var,  self.common_story_on_var),
            ('厳密',self.common_strict_var, self.common_strict_on_var),
        ]:
            r = ttk.Frame(cf)
            r.pack(fill='x', padx=2, pady=1)
            if chk_var:
                ttk.Checkbutton(r, text=lbl, variable=chk_var, width=6).pack(side='left')
            else:
                ttk.Label(r, text=lbl, width=6).pack(side='left')
            ttk.Entry(r, textvariable=var).pack(side='left', fill='x', expand=True, padx=(2, 0))

        # Prompt history
        hist_row = ttk.Frame(p)
        hist_row.pack(fill='x', padx=4, pady=(0, 2))
        ttk.Button(hist_row, text='📝 履歴', width=8, command=self._show_prompt_history).pack(side='left')
        ttk.Button(hist_row, text='🗑 履歴クリア', width=10, command=self._clear_prompt_history).pack(side='left', padx=2)
        self._prompt_history_count_var = tk.StringVar(value='(0件)')
        ttk.Label(hist_row, textvariable=self._prompt_history_count_var).pack(side='left', padx=2)

        # Generate / Stop bar
        act = ttk.Frame(p)
        act.pack(fill='x', padx=4, pady=4)
        self._gen_btn = ttk.Button(act, text='▶ 生成開始', command=self._generate_clicked)
        self._gen_btn.pack(side='left')
        self._stop_btn = ttk.Button(act, text='■ 停止', command=self._stop_clicked)
        self._stop_btn.pack(side='left', padx=4)
        self.pipe_label = ttk.Label(act, text='モデル: -')
        self.pipe_label.pack(side='left', padx=(8, 0))

    def _build_tab_config(self, p) -> None:
        # Scrollable for small screens
        canvas = tk.Canvas(p, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(p, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        _cw_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        # Sync inner frame width to canvas width
        canvas.bind('<Configure>', lambda e, c=canvas, w=_cw_id: c.itemconfigure(w, width=e.width))
        # Mouse wheel
        def _cfg_enter(e):
            self._active_scroll_canvas = canvas
        canvas.bind('<Enter>', _cfg_enter)
        inner.bind('<Enter>', _cfg_enter)

        PX = 4  # uniform padding

        # --- Model ---
        mf = ttk.LabelFrame(inner, text='モデル')
        mf.pack(fill='x', padx=PX, pady=PX)
        self.model_path_var = tk.StringVar(value='')
        r = ttk.Frame(mf); r.pack(fill='x', padx=2, pady=2)
        ttk.Entry(r, textvariable=self.model_path_var).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='…', width=2, command=self._browse_model).pack(side='left', padx=1)
        ttk.Button(r, text='自動', width=4, command=self._auto_detect_model).pack(side='left', padx=1)
        ttk.Button(r, text='読込', width=4, command=self._load_model_clicked).pack(side='left', padx=1)

        # --- Generation ---
        gf = ttk.LabelFrame(inner, text='生成設定')
        gf.pack(fill='x', padx=PX, pady=PX)
        self.prefix_var  = tk.StringVar(value='story')
        self.w_var       = tk.IntVar(value=1024)
        self.h_var       = tk.IntVar(value=1024)
        self.steps_var   = tk.IntVar(value=18)
        self.guidance_var= tk.DoubleVar(value=0.7)
        self.samples_var = tk.IntVar(value=4)
        self.cpu_offload_var = tk.BooleanVar(value=False)

        r = ttk.Frame(gf); r.pack(fill='x', padx=2, pady=2)
        for lbl, var, w in [('接頭辞',self.prefix_var,7),('幅',self.w_var,5),('高',self.h_var,5),
                             ('ステップ',self.steps_var,4),('強度',self.guidance_var,4),('枚数',self.samples_var,3)]:
            ttk.Label(r, text=lbl).pack(side='left', padx=(4, 1))
            ttk.Entry(r, textvariable=var, width=w).pack(side='left')
        self._chk_cpuoff = ttk.Checkbutton(r, text='CPU節約', variable=self.cpu_offload_var)
        self._chk_cpuoff.pack(side='left', padx=4)

        # --- Seed ---
        sf = ttk.LabelFrame(inner, text='シード')
        sf.pack(fill='x', padx=PX, pady=PX)
        self.random_seed_var = tk.BooleanVar(value=True)
        self.seed_var = tk.IntVar(value=-1)
        self.used_seed_var = tk.StringVar(value='-')
        r = ttk.Frame(sf); r.pack(fill='x', padx=2, pady=2)
        ttk.Checkbutton(r, text='ランダム', variable=self.random_seed_var, command=self._on_random_seed_toggle).pack(side='left')
        self.seed_entry = ttk.Entry(r, textvariable=self.seed_var, width=10)
        self.seed_entry.pack(side='left', padx=2)
        ttk.Label(r, text='使用:').pack(side='left', padx=(4, 1))
        self._used_seed_lbl = ttk.Label(r, textvariable=self.used_seed_var,
                                         cursor='hand2', foreground='#0066cc')
        self._used_seed_lbl.pack(side='left')
        self._used_seed_lbl.bind('<Button-1>', self._on_click_used_seed)
        self._on_random_seed_toggle()

        # --- Repair / Inpaint ---
        rf = ttk.LabelFrame(inner, text='修正 / 修復')
        rf.pack(fill='x', padx=PX, pady=PX)
        self.repair_steps_var    = tk.IntVar(value=24)
        self.repair_guidance_var = tk.DoubleVar(value=0.7)
        self.use_original_seed_var = tk.BooleanVar(value=True)

        r = ttk.Frame(rf); r.pack(fill='x', padx=2, pady=2)
        self._cfg_stp_lbl = ttk.Label(r, text='ステップ')
        self._cfg_stp_lbl.pack(side='left', padx=(0, 1))
        ttk.Entry(r, textvariable=self.repair_steps_var, width=4).pack(side='left')
        self._cfg_cfg_lbl = ttk.Label(r, text='強度')
        self._cfg_cfg_lbl.pack(side='left', padx=(4, 1))
        ttk.Entry(r, textvariable=self.repair_guidance_var, width=4).pack(side='left')
        ttk.Checkbutton(r, text='元seed', variable=self.use_original_seed_var).pack(side='left', padx=4)
        self._fix_skip_confirm_var = tk.BooleanVar(value=False)
        self._chk_fix_skip = ttk.Checkbutton(r, text='即修正', variable=self._fix_skip_confirm_var)
        self._chk_fix_skip.pack(side='left')

        self.repair_prefix_use_var  = tk.BooleanVar(value=False)
        self.inpaint_prefix_use_var = tk.BooleanVar(value=False)
        self.repair_prefix_var = tk.StringVar(value=(
            'Use image1 as the base. Keep the same scene and background. '
            'Only fix artifacts like broken anatomy, hands, faces, clothes, and extra parts. '
            'Do not add new people.'))
        self.inpaint_prefix_var = tk.StringVar(value=(
            'Reproduce image1 faithfully. '
            'The image has some painted-over / whited-out / damaged areas. '
            'Fix those areas to look natural, matching the surrounding art style, '
            'colors, and details. Keep everything else exactly as-is.'))
        self.inpaint_override_var    = tk.StringVar(value='')
        self.inpaint_use_full_ref_var= tk.BooleanVar(value=True)
        self.inpaint_mode_var        = tk.StringVar(value='full')  # full / crop
        self.inpaint_pad_pct_var     = tk.DoubleVar(value=0.15)
        self.inpaint_min_crop_var    = tk.IntVar(value=512)
        self.inpaint_mask_grow_var   = tk.IntVar(value=4)     # auto-mask dilation px
        self.inpaint_mask_blur_var   = tk.IntVar(value=8)     # soft edge blur px
        self.inpaint_damage_blur_var = tk.IntVar(value=0)     # blur damage edge before Klein

        r = ttk.Frame(rf); r.pack(fill='x', padx=2, pady=1)
        ttk.Checkbutton(r, text='修正指示', variable=self.repair_prefix_use_var).pack(side='left')
        ttk.Button(r, text='編集', command=self._edit_fix_prefix).pack(side='left', padx=2)

    def _build_tab_refs(self, p) -> None:
        top = ttk.Frame(p)
        top.pack(fill='x', padx=4, pady=4)
        self.ref_count_var = tk.StringVar(value='0')
        ttk.Label(top, textvariable=self.ref_count_var).pack(side='left')
        ttk.Button(top, text='追加…', command=self._add_refs).pack(side='left', padx=4)
        ttk.Button(top, text='クリア', command=self._clear_refs).pack(side='left')
        ttk.Button(top, text='一覧', command=self._show_ref_list).pack(side='left', padx=4)

        grid = ttk.Frame(p)
        grid.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        self.ref_file_labels = []
        self._ref_thumb_labels = []
        self._ref_thumb_imgs = []  # prevent GC
        for i in range(MAX_REFS):
            ttk.Label(grid, text=f'{i+1}').grid(row=i, column=0, sticky='w', padx=2, pady=1)
            thumb_lbl = tk.Label(grid, width=28, height=28, bg='#2a2a2a', relief='sunken')
            thumb_lbl.grid(row=i, column=1, sticky='w', padx=1, pady=1)
            self._ref_thumb_labels.append(thumb_lbl)
            self._ref_thumb_imgs.append(None)
            lbl = ttk.Label(grid, text='-', width=20)
            lbl.grid(row=i, column=2, sticky='w', padx=2, pady=1)
            self.ref_file_labels.append(lbl)
            ttk.Entry(grid, textvariable=self.ref_name_vars[i], width=18).grid(
                row=i, column=3, sticky='we', padx=2, pady=1)
        grid.columnconfigure(3, weight=1)

    # ---------- Inpaint tab: damage preview + run ----------

    def _build_tab_inpaint(self, p) -> None:
        # --- Top: current image name + status ---
        top = ttk.Frame(p)
        top.pack(fill='x', padx=4, pady=(4, 2))
        self._inp_status_var = tk.StringVar(value='画像が選択されていません')
        ttk.Label(top, textvariable=self._inp_status_var, wraplength=300).pack(side='left', fill='x', expand=True)

        # --- Buttons row ---
        btn_row = ttk.Frame(p)
        btn_row.pack(fill='x', padx=4, pady=2)
        ttk.Button(btn_row, text='🎨 ペインターを開く', command=self._open_mask_editor_for_current).pack(side='left')
        ttk.Button(btn_row, text='▶ 修復実行', command=self._inpaint_clicked).pack(side='left', padx=(8, 0))
        ttk.Button(btn_row, text='クリア', command=self._clear_damage).pack(side='right')

        # --- Damage preview (before/after side by side) ---
        prev_frame = ttk.LabelFrame(p, text='左: 元画像  /  右: ペイント済み（修復対象）')
        prev_frame.pack(fill='both', expand=True, padx=4, pady=2)

        self._inp_canvas = tk.Canvas(prev_frame, bg='#2a2a2a', highlightthickness=0)
        self._inp_canvas.pack(fill='both', expand=True)
        self._inp_canvas_img = None  # keep ref

        # --- Settings ---
        sf = ttk.LabelFrame(p, text='修復設定')
        sf.pack(fill='x', padx=4, pady=(2, 4))

        r = ttk.Frame(sf); r.pack(fill='x', padx=2, pady=1)
        ttk.Checkbutton(r, text='指示文', variable=self.inpaint_prefix_use_var).pack(side='left')
        ttk.Button(r, text='編集', command=self._edit_inpaint_prefix).pack(side='left', padx=2)
        ttk.Checkbutton(r, text='全参照', variable=self.inpaint_use_full_ref_var).pack(side='left', padx=(8, 0))

        r = ttk.Frame(sf); r.pack(fill='x', padx=2, pady=1)
        ttk.Label(r, text='上書き:').pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_override_var).pack(side='left', fill='x', expand=True, padx=2)

        r = ttk.Frame(sf); r.pack(fill='x', padx=2, pady=(1, 2))
        ttk.Label(r, text='方式:').pack(side='left')
        ttk.Combobox(r, textvariable=self.inpaint_mode_var, values=['full', 'crop'],
                      width=5, state='readonly').pack(side='left', padx=(1, 6))
        ttk.Label(r, text='余白%').pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_pad_pct_var, width=4).pack(side='left', padx=(1, 6))
        ttk.Label(r, text='最小切取').pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_min_crop_var, width=4).pack(side='left')

        r = ttk.Frame(sf); r.pack(fill='x', padx=2, pady=(1, 2))
        self._mask_grow_lbl = ttk.Label(r, text='膨張:')
        self._mask_grow_lbl.pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_mask_grow_var, width=3).pack(side='left', padx=(1, 6))
        self._mask_blur_lbl = ttk.Label(r, text='ぼかし:')
        self._mask_blur_lbl.pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_mask_blur_var, width=3).pack(side='left', padx=(1, 6))
        self._mask_dmg_lbl = ttk.Label(r, text='塗りぼかし:')
        self._mask_dmg_lbl.pack(side='left')
        ttk.Entry(r, textvariable=self.inpaint_damage_blur_var, width=3).pack(side='left')

    def _update_inpaint_tab(self) -> None:
        """Refresh the inpaint tab preview when current image changes."""
        cur = self.browser.current()
        if not cur or not cur.exists():
            self._inp_status_var.set('画像が選択されていません')
            self._inp_canvas.delete('all')
            self._inp_canvas_img = None
            return

        key = str(cur)
        has_damage = key in self._inpaint_damaged

        if has_damage:
            self._inp_status_var.set(f'✅ {cur.name} — damage painted, ready to inpaint')
        else:
            self._inp_status_var.set(f'⬜ {cur.name} — no damage yet (click "Open Damage Painter")')

        # Draw side-by-side preview
        try:
            self._inp_canvas.update_idletasks()
            cw = max(200, self._inp_canvas.winfo_width())
            ch = max(100, self._inp_canvas.winfo_height())

            orig = Image.open(cur).convert('RGB')
            iw, ih = orig.size

            if has_damage:
                damaged = self._inpaint_damaged[key]
                if damaged.size != (iw, ih):
                    damaged = damaged.resize((iw, ih), Image.LANCZOS)
                # Side by side: half width each
                half_w = cw // 2 - 4
                scale = min(half_w / iw, ch / ih, 1.0)
                tw = max(1, int(iw * scale))
                th = max(1, int(ih * scale))
                orig_r = orig.resize((tw, th), Image.LANCZOS)
                dam_r = damaged.resize((tw, th), Image.LANCZOS)
                # Compose side by side
                combo = Image.new('RGB', (tw * 2 + 8, th), (42, 42, 42))
                combo.paste(orig_r, (0, 0))
                combo.paste(dam_r, (tw + 8, 0))
            else:
                # Show original centered
                scale = min(cw / iw, ch / ih, 1.0)
                tw = max(1, int(iw * scale))
                th = max(1, int(ih * scale))
                combo = orig.resize((tw, th), Image.LANCZOS)

            tkimg = ImageTk.PhotoImage(combo)
            self._inp_canvas.delete('all')
            self._inp_canvas.create_image(cw // 2, ch // 2, anchor='center', image=tkimg)
            self._inp_canvas_img = tkimg  # prevent GC
        except Exception as e:
            self._inp_canvas.delete('all')
            self._inp_canvas_img = None
            self._inp_status_var.set(f'Preview error: {e}')

    def _clear_damage(self) -> None:
        """Remove damage data for the current image."""
        cur = self.browser.current()
        if not cur:
            return
        key = str(cur)
        self._inpaint_damaged.pop(key, None)
        self._inpaint_masks.pop(key, None)
        self._inpaint_mask_paths.pop(key, None)
        self._update_inpaint_tab()
        self._show_current_image()
        self.log(f'ペイントをクリア: {cur.name}')

    # ---------- NanoBanana (Gemini API) tab ----------

    def _build_tab_nanobanana(self, p) -> None:
        """Build the NanoBanana Pro (Gemini Image API) tab."""
        PX = 4  # uniform padding matching Config tab

        # Scrollable container (same pattern as Config tab)
        canvas = tk.Canvas(p, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(p, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        _nw_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        # Sync inner frame width to canvas width
        canvas.bind('<Configure>', lambda e, c=canvas, w=_nw_id: c.itemconfigure(w, width=e.width))
        # Mouse wheel
        def _nb_enter(e):
            self._active_scroll_canvas = canvas
        canvas.bind('<Enter>', _nb_enter)
        inner.bind('<Enter>', _nb_enter)

        # --- Warning ---
        ttk.Label(inner, text=(
            '⚠ NanoBanana Pro = Gemini Image API (Google)  '
            'APIキーはローカル設定に保存・自己責任'),
            foreground='orange').pack(anchor='w', padx=PX, pady=(PX, 2))

        # --- API key ---
        kf = ttk.LabelFrame(inner, text='APIキー')
        kf.pack(fill='x', padx=PX, pady=PX)
        r = ttk.Frame(kf); r.pack(fill='x', padx=2, pady=2)
        self.nb_api_key_var = tk.StringVar(value='')
        self._nb_key_entry = ttk.Entry(r, textvariable=self.nb_api_key_var, show='*')
        self._nb_key_entry.pack(side='left', fill='x', expand=True)
        self._nb_show_key = tk.BooleanVar(value=False)
        def _toggle_key():
            self._nb_key_entry.config(show='' if self._nb_show_key.get() else '*')
        ttk.Checkbutton(r, text='👁', variable=self._nb_show_key, command=_toggle_key).pack(side='left')

        # --- Model / Resolution / Aspect (single LabelFrame) ---
        cf = ttk.LabelFrame(inner, text='設定')
        cf.pack(fill='x', padx=PX, pady=PX)
        r = ttk.Frame(cf); r.pack(fill='x', padx=2, pady=2)
        ttk.Label(r, text='モデル:').pack(side='left')
        self.nb_model_var = tk.StringVar(value='gemini-3-pro-image-preview')
        ttk.Combobox(r, textvariable=self.nb_model_var, width=24, state='readonly',
                      values=['gemini-3-pro-image-preview',
                              'gemini-2.5-flash-image']).pack(side='left', padx=(1, 4))

        r = ttk.Frame(cf); r.pack(fill='x', padx=2, pady=(0, 2))
        ttk.Label(r, text='解像度:').pack(side='left')
        self.nb_resolution_var = tk.StringVar(value='2K')
        ttk.Combobox(r, textvariable=self.nb_resolution_var, width=4, state='readonly',
                      values=['1K', '2K', '4K']).pack(side='left', padx=(1, 6))
        ttk.Label(r, text='比率:').pack(side='left')
        self.nb_aspect_var = tk.StringVar(value='original')
        ttk.Combobox(r, textvariable=self.nb_aspect_var, width=8, state='readonly',
                      values=['original', '1:1', '16:9', '9:16', '4:3', '3:4',
                              '3:2', '2:3', '4:5', '5:4', '21:9']).pack(side='left', padx=1)

        # --- Mode ---
        mf = ttk.LabelFrame(inner, text='モード')
        mf.pack(fill='x', padx=PX, pady=PX)
        r = ttk.Frame(mf); r.pack(fill='x', padx=2, pady=2)
        self.nb_mode_var = tk.StringVar(value='refine')
        for m, lbl in [('refine', '🔧高品質化'), ('edit', '✏️編集'), ('generate', '🎨新規生成')]:
            ttk.Radiobutton(r, text=lbl, variable=self.nb_mode_var, value=m,
                             command=self._nb_mode_changed).pack(side='left', padx=2)
        self._nb_use_damaged_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='🎨ペイント版', variable=self._nb_use_damaged_var).pack(side='left', padx=(8, 0))

        # --- Prompt area ---
        pf = ttk.LabelFrame(inner, text='プロンプト')
        pf.pack(fill='x', padx=PX, pady=PX)

        self.nb_refine_prompt_var = tk.StringVar(value=(
            'Redraw this image with correct human anatomy. '
            'Fix all errors: wrong number of fingers, broken hands, '
            'distorted faces, extra limbs, merged body parts. '
            'Every person must have exactly 5 fingers on each hand. '
            'Keep the same composition, characters, style, and colors.'))
        self.nb_edit_prompt_var = tk.StringVar(value='')
        self.nb_gen_prompt_var = tk.StringVar(value='')

        self._nb_prompt_text = tk.Text(pf, height=4, wrap='word')
        self._nb_prompt_text.pack(fill='x', padx=2, pady=2)
        self._nb_prompt_text.insert('1.0', self.nb_refine_prompt_var.get())

        # Quick prompt presets
        qf = ttk.Frame(pf); qf.pack(fill='x', padx=2, pady=(0, 2))
        presets = [
            ('解剖修正', 'Redraw this image with correct human anatomy. '
                        'Fix all errors: wrong number of fingers, broken hands, '
                        'distorted faces, extra limbs, merged body parts. '
                        'Every person must have exactly 5 fingers per hand. '
                        'Keep the same composition, characters, style, and colors.'),
            ('清書', 'Redraw this illustration at professional quality. '
                    'Fix any anatomy errors, improve line quality, coloring, shading. '
                    'Correct any broken hands, faces, or body proportions. '
                    'Maintain the same characters, poses, scene, and art style.'),
            ('実写化', 'Convert this illustration into a photorealistic photograph. '
                      'Same composition, same scene, same characters but as real people '
                      'with correct anatomy and natural proportions.'),
            ('修復', 'This image has damaged/painted-over areas (colored patches). '
                    'Restore those areas naturally, matching the surrounding style. '
                    'Fix any anatomy errors in the restored areas. '
                    'Keep undamaged areas exactly as-is.'),
        ]
        for label, prompt in presets:
            ttk.Button(qf, text=label, width=8,
                       command=lambda pr=prompt: self._nb_set_prompt(pr)).pack(side='left', padx=1)

        # --- Extra refs + Run ---
        rf = ttk.LabelFrame(inner, text='参照 / 実行')
        rf.pack(fill='x', padx=PX, pady=PX)
        r = ttk.Frame(rf); r.pack(fill='x', padx=2, pady=2)
        self._nb_extra_refs = []
        ttk.Label(r, text='追加参照:').pack(side='left')
        self._nb_ref_label = ttk.Label(r, text='(0)')
        self._nb_ref_label.pack(side='left', padx=2)
        ttk.Button(r, text='追加', width=4, command=self._nb_add_ref).pack(side='left')
        ttk.Button(r, text='クリア', width=4, command=self._nb_clear_refs).pack(side='left', padx=2)

        r = ttk.Frame(rf); r.pack(fill='x', padx=2, pady=(0, 2))
        self._nb_run_btn = ttk.Button(r, text='🍌 NanoBananaに送信', command=self._nb_run)
        self._nb_run_btn.pack(side='left')
        self._nb_save_btn = ttk.Button(r, text='💾 保存', command=self._nb_save_result,
                                        state='disabled')
        self._nb_save_btn.pack(side='left', padx=(6, 0))
        self._nb_rerun_btn = ttk.Button(r, text='🔄 結果を再送信', command=self._nb_rerun_result,
                                         state='disabled')
        self._nb_rerun_btn.pack(side='left', padx=(6, 0))
        self._nb_ref_btn = ttk.Button(r, text='📌 参照にセット', command=self._nb_result_to_ref,
                                       state='disabled')
        self._nb_ref_btn.pack(side='left', padx=(6, 0))
        self._nb_status_var = tk.StringVar(value='待機中')
        ttk.Label(r, textvariable=self._nb_status_var).pack(side='left', padx=6)

        # --- Result preview ---
        rpf = ttk.LabelFrame(inner, text='結果')
        rpf.pack(fill='x', padx=PX, pady=PX)
        self._nb_result_canvas = tk.Canvas(rpf, bg='#2a2a2a', highlightthickness=0,
                                            height=200)
        self._nb_result_canvas.pack(fill='x', padx=2, pady=2)
        self._nb_result_canvas_img = None  # prevent GC

        # Store last result
        self._nb_result_img = None

    def _nb_mode_changed(self) -> None:
        """Switch prompt text when mode changes."""
        mode = self.nb_mode_var.get()
        # Save current prompt to its var
        self._nb_save_current_prompt()
        # Load prompt for new mode
        if mode == 'refine':
            self._nb_prompt_text.delete('1.0', 'end')
            self._nb_prompt_text.insert('1.0', self.nb_refine_prompt_var.get())
        elif mode == 'edit':
            self._nb_prompt_text.delete('1.0', 'end')
            self._nb_prompt_text.insert('1.0', self.nb_edit_prompt_var.get())
        else:
            self._nb_prompt_text.delete('1.0', 'end')
            self._nb_prompt_text.insert('1.0', self.nb_gen_prompt_var.get())

    def _nb_save_current_prompt(self) -> None:
        """Save current prompt text to the active mode's var."""
        txt = self._nb_prompt_text.get('1.0', 'end').strip()
        mode = self.nb_mode_var.get()
        if mode == 'refine':
            self.nb_refine_prompt_var.set(txt)
        elif mode == 'edit':
            self.nb_edit_prompt_var.set(txt)
        else:
            self.nb_gen_prompt_var.set(txt)

    def _nb_set_prompt(self, prompt: str) -> None:
        self._nb_prompt_text.delete('1.0', 'end')
        self._nb_prompt_text.insert('1.0', prompt)

    def _nb_add_ref(self) -> None:
        from tkinter import filedialog
        paths = filedialog.askopenfilenames(
            title='参照画像を選択',
            filetypes=[('Images', '*.png *.jpg *.jpeg *.webp')])
        for p in paths:
            if p and p not in self._nb_extra_refs:
                self._nb_extra_refs.append(p)
        self._nb_ref_label.config(text=f'({len(self._nb_extra_refs)})')

    def _nb_clear_refs(self) -> None:
        self._nb_extra_refs.clear()
        self._nb_ref_label.config(text='(0)')

    def _nb_show_result_preview(self) -> None:
        """Show NanoBanana result in the result canvas."""
        c = self._nb_result_canvas
        c.delete('all')
        self._nb_result_canvas_img = None
        if self._nb_result_img is None:
            return
        try:
            im = self._nb_result_img
            iw, ih = im.size
            c.update_idletasks()
            cw = max(100, c.winfo_width() - 4)
            scale = min(cw / iw, 1.0)
            tw = max(1, int(iw * scale))
            th = max(1, int(ih * scale))
            im2 = im.resize((tw, th), Image.LANCZOS) if scale < 1.0 else im
            tkimg = ImageTk.PhotoImage(im2)
            c.configure(height=th)
            c.create_image(cw // 2, 0, anchor='n', image=tkimg)
            self._nb_result_canvas_img = tkimg
        except Exception:
            pass

    def _nb_run(self) -> None:
        """Start NanoBanana API call in thread."""
        if self.is_generating:
            messagebox.showwarning('処理中', '生成中です。完了までお待ちください')
            return

        api_key = self.nb_api_key_var.get().strip()
        if not api_key:
            messagebox.showerror('NanoBanana', 'APIキーが必要です。\n'
                                 '取得先: https://aistudio.google.com/apikey')
            return

        prompt = self._nb_prompt_text.get('1.0', 'end').strip()
        if not prompt:
            messagebox.showwarning('NanoBanana', 'プロンプトを入力してください')
            return

        mode = self.nb_mode_var.get()
        self._nb_save_current_prompt()

        # Collect images
        images = []
        if mode in ('refine', 'edit'):
            cur = self.browser.current()
            if not cur or not cur.exists():
                messagebox.showwarning('NanoBanana', '修正/編集する画像が選択されていません')
                return

            # Check if user wants to send the damaged (painted) version
            use_damaged = self._nb_use_damaged_var.get()
            key = str(cur)
            if use_damaged and key in self._inpaint_damaged:
                img = self._inpaint_damaged[key].copy()
                self.log('NanoBanana: using damaged (painted) image')
            else:
                try:
                    img = Image.open(cur).convert('RGB')
                except Exception as e:
                    messagebox.showerror('NanoBanana', f'画像を開けませんでした: {e}')
                    return
                if use_damaged:
                    self.log('NanoBanana: no damaged image found, using original')
            images.append(img)

        # Extra ref images
        for ref_path in self._nb_extra_refs:
            try:
                img = Image.open(ref_path).convert('RGB')
                images.append(img)
            except Exception:
                pass

        self.is_generating = True
        self._nb_status_var.set('🔄 APIに送信中...')
        self._nb_run_btn.config(state='disabled')
        self._nb_rerun_btn.config(state='disabled')
        self._nb_ref_btn.config(state='disabled')
        self._open_progress('🍌 NanoBanana API')

        t = threading.Thread(target=self._nb_thread, args=(api_key, prompt, images), daemon=True)
        t.start()

    def _nb_thread(self, api_key: str, prompt: str, images: list) -> None:
        """Thread: call NanoBanana (Gemini) API."""
        import urllib.request
        import urllib.error
        import base64
        import io
        import time

        try:
            model = self.nb_model_var.get() or 'gemini-3-pro-image-preview'
            resolution = self.nb_resolution_var.get() or '2K'
            aspect = self.nb_aspect_var.get() or 'original'

            t0 = time.time()
            self._log_threadsafe(f'--- NanoBanana API ({model}) ---')
            self._log_threadsafe(f'  resolution={resolution}, aspect={aspect}, images={len(images)}')
            self._log_threadsafe(f'  prompt: {prompt[:120]}{"..." if len(prompt)>120 else ""}')

            # Build parts
            parts = []
            parts.append({'text': prompt})
            for img in images:
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                b64 = base64.b64encode(buf.getvalue()).decode('ascii')
                parts.append({
                    'inline_data': {
                        'mime_type': 'image/png',
                        'data': b64,
                    }
                })

            # Build body
            body = {
                'contents': [{
                    'parts': parts
                }],
                'generationConfig': {
                    'responseModalities': ['TEXT', 'IMAGE'],
                }
            }

            # Add image config if not default
            img_config = {}
            if aspect != 'original':
                img_config['aspectRatio'] = aspect
            if resolution:
                img_config['imageSize'] = resolution
            if img_config:
                body['generationConfig']['imageConfig'] = img_config

            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

            data = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('x-goog-api-key', api_key)

            self._log_threadsafe(f'  request size: {len(data) / 1024:.0f} KB')

            with urllib.request.urlopen(req, timeout=300) as resp:
                resp_data = resp.read()

            result = json.loads(resp_data)
            dt = time.time() - t0

            # Parse response
            text_parts = []
            result_img = None

            candidates = result.get('candidates', [])
            if not candidates:
                # Check for error
                err = result.get('error', {})
                if err:
                    raise RuntimeError(f"API error: {err.get('message', str(err))}")
                # Check for prompt feedback (blocked)
                feedback = result.get('promptFeedback', {})
                block_reason = feedback.get('blockReason', '')
                if block_reason:
                    raise RuntimeError(f"Blocked by safety filter: {block_reason}")
                raise RuntimeError(f"No candidates in response: {json.dumps(result)[:300]}")

            content = candidates[0].get('content', {})
            for part in content.get('parts', []):
                if 'text' in part:
                    text_parts.append(part['text'])
                elif 'inlineData' in part:
                    img_data = base64.b64decode(part['inlineData']['data'])
                    result_img = Image.open(io.BytesIO(img_data))

            if text_parts:
                self._log_threadsafe(f'  API text: {" ".join(text_parts)[:200]}')

            if result_img is None:
                raise RuntimeError('No image in API response. ' +
                                   ('Text: ' + ' '.join(text_parts)[:200] if text_parts else ''))

            self._nb_result_img = result_img
            self._log_threadsafe(f'  result: {result_img.size[0]}x{result_img.size[1]} ({dt:.1f}s)')

            def _on_done():
                self._close_progress()
                self._nb_status_var.set(f'✅ Done ({result_img.size[0]}x{result_img.size[1]}, {dt:.1f}s)')
                self._nb_save_btn.config(state='normal')
                self._nb_run_btn.config(state='normal')
                self._nb_rerun_btn.config(state='normal')
                self._nb_ref_btn.config(state='normal')
                self._nb_show_result_preview()
            self.after(0, _on_done)

        except urllib.error.HTTPError as e:
            err_body = ''
            try:
                err_body = e.read().decode('utf-8', errors='ignore')[:500]
            except Exception:
                pass
            err_code = e.code
            msg = f'HTTP {err_code}: {err_body or e.reason}'
            self._log_threadsafe(f'  NanoBanana ERROR: {msg}')

            def _on_err(c=err_code, m=msg):
                self._close_progress()
                self._nb_status_var.set(f'❌ Error ({c})')
                self._nb_run_btn.config(state='normal')
                if self._nb_result_img is not None:
                    self._nb_rerun_btn.config(state='normal')
                    self._nb_ref_btn.config(state='normal')
                messagebox.showerror('NanoBanana API Error', m[:600])
            self.after(0, _on_err)

        except Exception as e:
            err_msg = str(e)
            self._log_threadsafe(f'  NanoBanana ERROR: {err_msg}')

            def _on_err(m=err_msg):
                self._close_progress()
                self._nb_status_var.set(f'❌ Error')
                self._nb_run_btn.config(state='normal')
                if self._nb_result_img is not None:
                    self._nb_rerun_btn.config(state='normal')
                    self._nb_ref_btn.config(state='normal')
                messagebox.showerror('NanoBanana Error', m[:600])
            self.after(0, _on_err)

        self.is_generating = False
        self.after(0, self._close_progress)  # safety net

    def _nb_save_result(self) -> None:
        """Save NanoBanana result to outputs directory."""
        if self._nb_result_img is None:
            return

        self.job_counter += 1
        self.after(0, self._schedule_autosave)

        cur = self.browser.current()
        base_name = cur.stem if cur else 'nb'
        model_short = 'pro' if 'pro' in self.nb_model_var.get() else 'flash'
        fname = f'{self.prefix_var.get().strip() or "story"}_{self.job_counter:04d}_{_now_stamp()}_nb{model_short}.png'
        out_path = self.paths.outputs_dir / fname
        self._nb_result_img.save(out_path)

        # Save sidecar
        prompt = self._nb_prompt_text.get('1.0', 'end').strip()
        self._save_sidecar(
            png_path=out_path,
            raw_line=f'NanoBanana {self.nb_mode_var.get()} from {base_name}',
            common='',
            resolved=prompt,
            used_seed=0,
            pipe_name=self.nb_model_var.get(),
            ref_used=[],
            ref_desc=f'NanoBanana {self.nb_model_var.get()}',
            mode=f'nanobanana-{self.nb_mode_var.get()}',
            base_image=cur.name if cur else '',
        )

        self.log(f'NanoBanana saved: {out_path.name}')
        self.after(0, self._rescan_outputs)
        self._nb_status_var.set(f'💾 保存済: {fname}')

    def _nb_rerun_result(self) -> None:
        """NB結果画像をソースとして再度NanoBananaに送信"""
        if self._nb_result_img is None:
            messagebox.showwarning('NanoBanana', '再送信する結果画像がありません')
            return
        if self.is_generating:
            messagebox.showwarning('処理中', '生成中です。完了までお待ちください')
            return

        api_key = self._nb_api_key_var.get().strip()
        if not api_key:
            messagebox.showerror('NanoBanana', 'APIキーが必要です。\n'
                                  '取得先: https://aistudio.google.com/apikey')
            return
        prompt = self._nb_prompt_text.get('1.0', 'end').strip()
        if not prompt:
            messagebox.showwarning('NanoBanana', 'プロンプトを入力してください')
            return

        # Build image list from result image
        images = []
        import io as _io
        buf = _io.BytesIO()
        self._nb_result_img.save(buf, format='PNG')
        images.append(('image/png', buf.getvalue()))

        # Add extra refs
        for rp in self._nb_extra_refs:
            try:
                with open(rp, 'rb') as f:
                    data = f.read()
                mt = 'image/jpeg' if str(rp).lower().endswith(('.jpg', '.jpeg')) else 'image/png'
                images.append((mt, data))
            except Exception:
                pass

        self.is_generating = True
        self._nb_run_btn.config(state='disabled')
        self._nb_save_btn.config(state='disabled')
        self._nb_rerun_btn.config(state='disabled')
        self._nb_ref_btn.config(state='disabled')
        self._nb_status_var.set('🔄 再送信中...')
        self._open_progress('🍌 NanoBanana 再送信')
        threading.Thread(target=self._nb_thread, args=(api_key, prompt, images),
                         daemon=True).start()

    def _nb_result_to_ref(self) -> None:
        """NB結果画像を一時保存して参照画像スロットにセット"""
        if self._nb_result_img is None:
            return
        try:
            tmp_path = self.paths.tmp_dir / f'nb_result_{_now_stamp()}.png'
            self._nb_result_img.save(tmp_path)
            for i in range(MAX_REFS):
                if not self.ref_paths[i]:
                    self.ref_paths[i] = str(tmp_path)
                    self._update_ref_ui()
                    self._schedule_autosave()
                    self.log(f'NB結果を参照画像{i+1}にセット')
                    return
            # All full → replace last
            self.ref_paths[MAX_REFS - 1] = str(tmp_path)
            self._update_ref_ui()
            self._schedule_autosave()
            self.log(f'NB結果を参照画像{MAX_REFS}に上書きセット')
        except Exception as e:
            self.log(f'[WARN] NB→参照セット失敗: {e}')

    # ---------- Right panel: Preview + Log ----------

    def _build_right_panel(self, parent) -> None:
        # --- Navigation row (compact) ---
        nav = ttk.Frame(parent)
        nav.pack(fill='x', padx=4, pady=(4, 0))

        self._btn_prev = ttk.Button(nav, text='◀', width=2, command=self._prev_image)
        self._btn_prev.pack(side='left')
        self._btn_next = ttk.Button(nav, text='▶', width=2, command=self._next_image)
        self._btn_next.pack(side='left', padx=1)
        self._btn_rescan = ttk.Button(nav, text='⟳', width=2, command=self._rescan_outputs)
        self._btn_rescan.pack(side='left', padx=(2, 4))

        self._chk_latest = ttk.Checkbutton(nav, text='最新', variable=self.scan_latest_var,
                         command=self._rescan_outputs)
        self._chk_latest.pack(side='left')
        self._spn_latest = ttk.Spinbox(nav, from_=50, to=5000, increment=50, width=5,
                     textvariable=self.scan_latest_n_var, command=self._rescan_outputs)
        self._spn_latest.pack(side='left', padx=1)

        self.filter_combo = ttk.Combobox(nav, textvariable=self.output_filter_var, width=12,
                                          state='readonly', values=['全て','Good','Star','Bad','未評価'])
        self.filter_combo.pack(side='left', padx=2)
        self.filter_combo.bind('<<ComboboxSelected>>', self._on_filter_changed)

        ttk.Separator(nav, orient='vertical').pack(side='left', fill='y', padx=3)
        self._btn_fix = ttk.Button(nav, text='修正', command=self._repair_clicked)
        self._btn_fix.pack(side='left')

        # --- Info + mark row (BEFORE preview so it's always visible) ---
        info_row = ttk.Frame(parent)
        info_row.pack(fill='x', padx=4, pady=(2, 0))
        self.preview_info = ttk.Label(info_row, text='-', anchor='w')
        self.preview_info.pack(side='left', fill='x', expand=True)
        self._chk_fit = ttk.Checkbutton(info_row, text='全体', variable=self._preview_zoom_fit,
                         command=self._show_current_image)
        self._chk_fit.pack(side='left', padx=(0, 4))
        self._mark_btns = {}
        for txt, cmd in [('G',self._mark_current_good),('S',self._mark_current_star),
                          ('B',self._mark_current_bad),('U',self._clear_current_mark)]:
            b = ttk.Button(info_row, text=txt, width=2, command=cmd)
            b.pack(side='left', padx=1)
            self._mark_btns[txt] = b

        # --- Navigation slider (jump to any image) ---
        slider_row = ttk.Frame(parent)
        slider_row.pack(fill='x', padx=4, pady=(0, 0))
        self._nav_slider_var = tk.IntVar(value=1)
        self._nav_slider = ttk.Scale(slider_row, from_=1, to=1, orient='horizontal',
                                      variable=self._nav_slider_var,
                                      command=self._on_nav_slider)
        self._nav_slider.pack(fill='x')
        self._nav_slider_updating = False  # guard flag

        # --- Preview: 100% Canvas with scroll ---
        self.preview_frame = ttk.Frame(parent)
        self.preview_frame.pack(fill='both', expand=True, padx=4, pady=2)

        self._preview_canvas = tk.Canvas(self.preview_frame, bg='#1e1e1e',
                                          highlightthickness=0)
        self._pv_vscroll = ttk.Scrollbar(self.preview_frame, orient='vertical',
                                          command=self._preview_canvas.yview)
        self._pv_hscroll = ttk.Scrollbar(self.preview_frame, orient='horizontal',
                                          command=self._preview_canvas.xview)
        self._preview_canvas.configure(yscrollcommand=self._pv_vscroll.set,
                                        xscrollcommand=self._pv_hscroll.set)

        self._pv_hscroll.pack(side='bottom', fill='x')
        self._pv_vscroll.pack(side='right', fill='y')
        self._preview_canvas.pack(side='left', fill='both', expand=True)

        self._preview_tk_img = None  # prevent GC

        # Mouse wheel scroll — set active canvas on enter
        def _pv_enter(e):
            self._active_scroll_canvas = self._preview_canvas
        self._preview_canvas.bind('<Enter>', _pv_enter)

        # Double-click → open in OS viewer
        self._preview_canvas.bind('<Double-Button-1>', lambda e: self._open_current_in_viewer())

        # Right-click → context menu
        self._preview_ctx_menu = tk.Menu(self, tearoff=0)
        self._preview_ctx_menu.add_command(label='🔍 外部ビューアで開く', command=self._open_current_in_viewer)
        self._preview_ctx_menu.add_command(label='📁 フォルダを開く', command=self._open_outputs)
        self._preview_ctx_menu.add_separator()
        self._preview_ctx_menu.add_command(label='📋 ファイルパスをコピー', command=self._copy_current_path)
        self._preview_ctx_menu.add_command(label='📋 プロンプトをコピー', command=self._copy_resolved_clicked)
        self._preview_ctx_menu.add_separator()
        self._preview_ctx_menu.add_command(label='📌 参照画像にセット', command=self._set_current_as_ref)
        self._preview_ctx_menu.add_command(label='🍌 NanoBananaに送信', command=self._send_current_to_nb)
        self._preview_ctx_menu.add_command(label='🗑 この画像を削除', command=self._delete_current_image)
        def _show_ctx(e):
            try:
                self._preview_ctx_menu.tk_popup(e.x_root, e.y_root)
            finally:
                self._preview_ctx_menu.grab_release()
        self._preview_canvas.bind('<Button-3>', _show_ctx)

        # --- Bottom section: Prompt + Log (fixed height, won't be pushed out) ---
        bottom = ttk.Frame(parent)
        bottom.pack(fill='x', padx=4, pady=(0, 2))

        # Prompt viewer (collapsible)
        self._prompt_visible = tk.BooleanVar(value=False)
        tog = ttk.Frame(bottom)
        tog.pack(fill='x', pady=(2, 0))
        ttk.Checkbutton(tog, text='プロンプト表示', variable=self._prompt_visible,
                         command=self._toggle_prompt_view).pack(side='left')
        self._btn_cp_raw = ttk.Button(tog, text='生コピー', command=self._copy_raw_clicked)
        self._btn_cp_raw.pack(side='left', padx=2)
        self._btn_cp_res = ttk.Button(tog, text='解決コピー', command=self._copy_resolved_clicked)
        self._btn_cp_res.pack(side='left', padx=1)
        self._btn_cp_fix = ttk.Button(tog, text='修正コピー', command=self._copy_fix_prompt_clicked)
        self._btn_cp_fix.pack(side='left', padx=1)
        self._btn_add_raw = ttk.Button(tog, text='+生追加', command=self._append_raw_to_prompts_clicked)
        self._btn_add_raw.pack(side='left', padx=1)
        self._btn_add_res = ttk.Button(tog, text='+解決追加', command=self._append_resolved_to_prompts_clicked)
        self._btn_add_res.pack(side='left', padx=1)

        self._prompt_frame = ttk.Frame(bottom)
        # starts hidden
        self.raw_view = tk.Text(self._prompt_frame, height=2, wrap='word', state='disabled')
        self.raw_view.pack(fill='x', pady=1)
        self.resolved_view = tk.Text(self._prompt_frame, height=3, wrap='word', state='disabled')
        self.resolved_view.pack(fill='x', pady=(0, 2))

        # Log (compact)
        self.log_text = tk.Text(bottom, height=4, state='disabled')
        self.log_text.pack(fill='x', pady=(2, 2))

        ttk.Label(bottom, text='キー: ←→=前後  Home/End=先頭/末尾  G/Space=Good  S=Star  B=Bad  U=解除  Z=全体/100%  Del=削除  Ctrl+S=保存  Ctrl+🖱=前後',
                  font=('', 7)).pack(anchor='w', pady=(0, 1))

        self.log(f'TMP: {self.paths.tmp_dir}')
        self.log(f'OUT: {self.paths.outputs_dir}')

    def _toggle_prompt_view(self) -> None:
        if self._prompt_visible.get():
            self._prompt_frame.pack(fill='x', padx=0, pady=0, before=self.log_text)
        else:
            self._prompt_frame.pack_forget()

    # ==================== P2: Key bindings (dict dispatch) ====================

    def _setup_keybinds(self) -> None:
        binds = {
            '<Left>':      ('prev',   self._prev_image),
            '<Right>':     ('next',   self._next_image),
            '<Return>':    ('next',   self._next_image),
            '<KP_Enter>':  ('next',   self._next_image),
            '<BackSpace>': ('prev',   self._prev_image),
            '<space>':     ('good',   self._mark_current_good),
            '<g>':         ('good',   self._mark_current_good),
            '<G>':         ('good',   self._mark_current_good),
            '<b>':         ('bad',    self._mark_current_bad),
            '<B>':         ('bad',    self._mark_current_bad),
            '<s>':         ('star',   self._mark_current_star),
            '<S>':         ('star',   self._mark_current_star),
            '<u>':         ('clear',  self._clear_current_mark),
            '<U>':         ('clear',  self._clear_current_mark),
            '<c>':         ('copy',   self._copy_resolved_clicked),
            '<C>':         ('copy',   self._copy_resolved_clicked),
            '<f>':         ('fix',    self._copy_fix_prompt_clicked),
            '<F>':         ('fix',    self._copy_fix_prompt_clicked),
            '<e>':         ('export', self._on_e_action),
            '<E>':         ('export', self._on_e_action),
            '<z>':         ('zoom',   self._toggle_preview_zoom),
            '<Z>':         ('zoom',   self._toggle_preview_zoom),
            '<Home>':      ('home',   self._first_image),
            '<End>':       ('end',    self._last_image),
            '<r>':         ('repair', self._repair_clicked),
            '<R>':         ('repair', self._repair_clicked),
        }
        for key, (debounce_id, action) in binds.items():
            self.bind_all(key, self._make_key_handler(debounce_id, action), add='+')

        # Ctrl+S: save settings (works even in text widgets)
        self.bind_all('<Control-s>', lambda e: self._save_settings_clicked())
        self.bind_all('<Control-S>', lambda e: self._save_settings_clicked())

        # Delete key: delete current image
        self.bind_all('<Delete>', lambda e: self._delete_current_image()
                      if not _is_text_focus_widget(self.focus_get()) else None)

    def _make_key_handler(self, debounce_id: str, action):
        def handler(event):
            if _is_text_focus_widget(self.focus_get()):
                return
            if not self._key_allowed(debounce_id):
                return
            action()
        return handler

    def _key_allowed(self, key: str, min_interval: float = 0.08) -> bool:
        """Debounce key repeat to avoid rapid-fire actions."""
        now = time.time()  # P6: use top-level time, no redundant import
        last = self._last_key_ts.get(key, 0.0)
        if (now - last) < min_interval:
            return False
        self._last_key_ts[key] = now
        return True

    def _on_e_action(self) -> None:
        """Export depends on current filter: if Star filter, export star, else export good."""
        try:
            mode = (self.output_filter_var.get() or '').strip()
            mode_clean = mode.split('(')[0].strip() if '(' in mode else mode
        except Exception:
            mode_clean = ''
        if mode_clean == 'Star':
            self._export_star_images()
        else:
            self._export_good_images()

    # ==================== Model management ====================

    def _browse_model(self) -> None:
        d = filedialog.askdirectory(title='モデルフォルダを選択')
        if d:
            self.model_path_var.set(d)

    def _auto_detect_model(self) -> None:
        p = detect_4b_model_dir(self.paths.models_dir)
        if p:
            self.model_path_var.set(str(p))
            self.log(f'自動検出: {p}')
        else:
            self.log('自動検出: 見つかりません')

    def _load_model_clicked(self) -> None:
        path = (self.model_path_var.get() or '').strip()
        if not path:
            messagebox.showerror('モデル', 'モデルパスが空です')
            return
        try:
            self.engine.load(Path(path), cpu_offload=self.cpu_offload_var.get())
            self.pipe_label.configure(text=f'モデル: {self.engine.pipe_name}')
        except Exception as e:
            messagebox.showerror('モデル読込', str(e))
            self.log(f'読み込みエラー: {e}')

    def _auto_bootstrap(self) -> None:
        try:
            preferred = (self.model_path_var.get() or '').strip()
            p = Path(preferred) if preferred and Path(preferred).exists() else None
            if p is None:
                p = detect_4b_model_dir(self.paths.models_dir)
            if not p:
                p = auto_install_4b(self.paths.models_dir, log_cb=self._log_threadsafe)
            self.model_path_var.set(str(p))
            self.engine.load(Path(p), cpu_offload=self.cpu_offload_var.get())
            self.pipe_label.configure(text=f'モデル: {self.engine.pipe_name}')
        except Exception as e:
            self.log(f'自動起動エラー: {e}')
            messagebox.showwarning('モデル未準備', f'{e}\n\nTMP/models にモデルフォルダを配置するか\n設定タブの「…」ボタンで選択してください')

        self._rescan_outputs()
        self._show_current_image()

    # ==================== Reference images ====================

    def _update_ref_ui(self) -> None:
        count = 0
        for i in range(MAX_REFS):
            p = self.ref_paths[i]
            if p:
                count += 1
                self.ref_file_labels[i].configure(text=Path(p).name)
                # Update thumbnail
                try:
                    im = Image.open(p).convert('RGB')
                    im.thumbnail((28, 28), Image.LANCZOS)
                    tkimg = ImageTk.PhotoImage(im)
                    self._ref_thumb_labels[i].configure(image=tkimg)
                    self._ref_thumb_imgs[i] = tkimg  # prevent GC
                except Exception:
                    self._ref_thumb_labels[i].configure(image='')
                    self._ref_thumb_imgs[i] = None
            else:
                self.ref_file_labels[i].configure(text='-')
                self._ref_thumb_labels[i].configure(image='')
                self._ref_thumb_imgs[i] = None
        self.ref_count_var.set(f'{count} ref')

    def _add_refs(self) -> None:
        files = filedialog.askopenfilenames(
            title='参照画像を選択',
            filetypes=[('Images', '*.png;*.jpg;*.jpeg;*.webp;*.bmp'), ('All', '*.*')]
        )
        if not files:
            return
        for f in files:
            placed = False
            for i in range(MAX_REFS):
                if not self.ref_paths[i]:
                    self.ref_paths[i] = f
                    placed = True
                    break
            if not placed:
                break
        self._update_ref_ui()
        self._schedule_autosave()

    def _clear_refs(self) -> None:
        self.ref_paths = [None] * MAX_REFS
        for v in self.ref_name_vars:
            v.set('')
        self._update_ref_ui()
        self._schedule_autosave()

    def _show_ref_list(self) -> None:
        lines = []
        for i in range(MAX_REFS):
            p = self.ref_paths[i]
            name = (self.ref_name_vars[i].get() or '').strip()
            if p:
                lines.append(f'image{i+1}: {Path(p).name}  name={name}')
        messagebox.showinfo('参照画像', '\n'.join(lines) if lines else '参照画像なし')

    def _load_ref_images(self, used_indices: Optional[List[int]] = None) -> List[Image.Image]:
        imgs: List[Image.Image] = []
        indices = used_indices or list(range(1, MAX_REFS + 1))
        for idx in indices:
            p = self.ref_paths[idx - 1]
            if not p:
                continue
            try:
                imgs.append(Image.open(p).convert('RGB'))
            except Exception as e:
                self.log(f'参照画像の読み込み失敗 (image{idx}): {e}')
        return imgs

    def _on_random_seed_toggle(self) -> None:
        if self.random_seed_var.get():
            self.seed_var.set(-1)
            self.seed_entry.configure(state='disabled')
        else:
            self.seed_entry.configure(state='normal')

    def _on_click_used_seed(self, event=None) -> None:
        """使用seedをクリック → クリップボードにコピー＆seedフィールドにセット"""
        val = self.used_seed_var.get()
        if not val or val == '-':
            return
        try:
            seed_int = int(val)
            self.seed_var.set(seed_int)
            self.random_seed_var.set(False)
            self._on_random_seed_toggle()
            self._copy_to_clipboard(val)
            self.log(f'Seedをセット: {val}（ランダムOFF）')
        except ValueError:
            pass

    # ==================== Prefix editors ====================

    def _edit_fix_prefix(self) -> None:
        self._open_multiline_editor(title='修正指示プレフィックス', var=self.repair_prefix_var)

    def _edit_inpaint_prefix(self) -> None:
        self._open_multiline_editor(title='修復指示プレフィックス', var=self.inpaint_prefix_var)

    def _show_prompt_history(self) -> None:
        """プロンプト履歴をダイアログ表示。クリックで再利用"""
        if not self._prompt_history:
            messagebox.showinfo('履歴', 'プロンプト履歴がありません')
            return

        top = tk.Toplevel(self)
        top.title('プロンプト履歴')
        top.geometry('500x400')
        top.transient(self)

        ttk.Label(top, text='クリックでプロンプト欄に追加（ダブルクリックで置換）').pack(padx=4, pady=(4, 2))

        listbox = tk.Listbox(top, selectmode='single', font=('', 9))
        listbox.pack(fill='both', expand=True, padx=4, pady=2)
        sb = ttk.Scrollbar(listbox, orient='vertical', command=listbox.yview)
        listbox.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')

        for h in self._prompt_history:
            listbox.insert('end', h[:120])

        def _append(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            text = self._prompt_history[sel[0]]
            self._append_text_to_prompts(text)

        def _replace(event=None):
            sel = listbox.curselection()
            if not sel:
                return
            text = self._prompt_history[sel[0]]
            self.prompts_text.delete('1.0', 'end')
            self.prompts_text.insert('1.0', text)
            self.prompts_text.focus_set()

        listbox.bind('<Return>', _append)
        listbox.bind('<Double-Button-1>', _replace)

        btn_row = ttk.Frame(top)
        btn_row.pack(fill='x', padx=4, pady=(2, 4))
        ttk.Button(btn_row, text='追加', command=_append).pack(side='left')
        ttk.Button(btn_row, text='置換', command=_replace).pack(side='left', padx=2)
        ttk.Button(btn_row, text='閉じる', command=top.destroy).pack(side='right')

    def _clear_prompt_history(self) -> None:
        if messagebox.askyesno('履歴', 'プロンプト履歴をクリアしますか？'):
            self._prompt_history.clear()
            self._prompt_history_count_var.set('(0件)')
            self.log('プロンプト履歴をクリア')

    def _open_multiline_editor(self, title: str, var: tk.StringVar) -> None:
        top = tk.Toplevel(self)
        top.title(title)
        top.transient(self)
        top.grab_set()
        top.geometry('720x360')

        frm = ttk.Frame(top)
        frm.pack(fill='both', expand=True, padx=8, pady=8)

        txt = tk.Text(frm, wrap='word')
        txt.pack(fill='both', expand=True)
        txt.insert('1.0', var.get() or '')

        btns = ttk.Frame(frm)
        btns.pack(fill='x', pady=(8, 0))

        def _save():
            var.set(txt.get('1.0', 'end-1c'))
            top.destroy()

        ttk.Button(btns, text='保存', command=_save).pack(side='left')
        ttk.Button(btns, text='キャンセル', command=top.destroy).pack(side='left', padx=6)

    # ==================== Output filtering & browsing ====================

    def _apply_output_filter(self, keep_current: bool = True) -> None:
        """Rescan outputs and apply current filter to browser.files."""
        cur_path = self.browser.current() if keep_current else None
        self._total_png_count = 0  # track unfiltered total
        try:
            if not self.paths.outputs_dir.exists():
                self.browser.files = []
                self.browser.index = -1
                self._update_filter_counts({})
                return
            pngs = sorted(self.paths.outputs_dir.glob('*.png'), key=lambda p: p.stat().st_mtime)
            # Speed: optionally scan only the latest N files
            try:
                if bool(self.scan_latest_var.get()):
                    n = int(self.scan_latest_n_var.get())
                    if n > 0 and len(pngs) > n:
                        pngs = pngs[-n:]
            except Exception:
                pass
            self._total_png_count = len(pngs)

            # Count flags for all images (used for filter label display)
            flag_counts = {'good': 0, 'star': 0, 'bad': 0, 'none': 0}
            flag_map = {}  # path → flag
            for p in pngs:
                meta = self._read_sidecar(p)
                flag = (meta.get('flag') or '').strip()
                flag_map[p] = flag
                if flag in ('good', 'star', 'bad'):
                    flag_counts[flag] += 1
                else:
                    flag_counts['none'] += 1

            mode = (self.output_filter_var.get() or '全て').strip()
            # Strip count suffix for matching (e.g. "Good (5)" → "Good")
            mode_clean = mode.split('(')[0].strip() if '(' in mode else mode
            if mode_clean in ('All', '全て', ''):
                flt = pngs
            else:
                want_map = {'Good': 'good', 'Star': 'star', 'Bad': 'bad', 'None': None, '未評価': None}
                want = want_map.get(mode_clean, '')
                flt = []
                for p in pngs:
                    flag = flag_map.get(p, '')
                    if want is None:
                        if not flag:
                            flt.append(p)
                    else:
                        if flag == want:
                            flt.append(p)
            self.browser.files = flt
            self._update_filter_counts(flag_counts)
            if not flt:
                self.browser.index = -1
                return
            if cur_path and cur_path in flt:
                self.browser.index = flt.index(cur_path)
            else:
                self.browser.index = len(flt) - 1
        except Exception as e:
            self.log(f'[WARN] filter scan failed: {e}')
            try:
                self.browser.rescan()
            except Exception:
                self.browser.files = []
                self.browser.index = -1

    def _update_filter_counts(self, counts: dict) -> None:
        """Update filter combobox values with counts."""
        total = sum(counts.values()) if counts else 0
        g = counts.get('good', 0)
        s = counts.get('star', 0)
        b = counts.get('bad', 0)
        n = counts.get('none', 0)
        vals = [
            f'全て ({total})',
            f'Good ({g})',
            f'Star ({s})',
            f'Bad ({b})',
            f'未評価 ({n})',
        ]
        try:
            self.filter_combo.configure(values=vals)
        except Exception:
            pass

    def _rescan_outputs(self) -> None:
        self._apply_output_filter(keep_current=False)
        self._show_current_image()

    def _on_filter_changed(self, event=None) -> None:
        self._apply_output_filter()
        self._show_current_image()

    # ==================== Sidecar (metadata) ====================

    def _read_sidecar(self, png_path: Path) -> dict:
        txt = png_path.with_suffix('.txt')
        data = {
            'raw_line': '',
            'common': '',
            'resolved': '',
            'used_seed': None,
            'flag': '',
        }
        if not txt.exists():
            return data

        # Cache by sidecar path + mtime
        try:
            key = str(txt)
            mtime = txt.stat().st_mtime
            cached = self._sidecar_cache.get(key)  # P6: no hasattr check needed
            if cached and cached[0] == mtime:
                try:
                    self._sidecar_cache.move_to_end(key)
                except Exception:
                    pass
                return dict(cached[1])
        except Exception:
            cached = None

        try:
            s_lines = txt.read_text(encoding='utf-8', errors='ignore').splitlines()
            for line in s_lines:
                if line.startswith('raw_line:'):
                    data['raw_line'] = line.replace('raw_line:', '', 1).strip()
                elif line.startswith('common:'):
                    data['common'] = line.replace('common:', '', 1).strip()
                elif line.startswith('resolved:'):
                    data['resolved'] = line.replace('resolved:', '', 1).strip()
                elif line.startswith('used_seed:'):
                    v = line.replace('used_seed:', '', 1).strip()
                    try:
                        data['used_seed'] = int(v)
                    except ValueError:
                        data['used_seed'] = None
                elif line.startswith('flag:'):
                    data['flag'] = line.replace('flag:', '', 1).strip()
                elif line.startswith('steps:'):
                    data['steps'] = line.replace('steps:', '', 1).strip()
                elif line.startswith('guidance:'):
                    data['guidance'] = line.replace('guidance:', '', 1).strip()
                elif line.startswith('width:'):
                    data['width'] = line.replace('width:', '', 1).strip()
                elif line.startswith('height:'):
                    data['height'] = line.replace('height:', '', 1).strip()
                elif line.startswith('pipe:'):
                    data['pipe'] = line.replace('pipe:', '', 1).strip()
                elif line.startswith('mode:'):
                    data['mode'] = line.replace('mode:', '', 1).strip()
        except Exception as e:
            self.log(f'[WARN] sidecar read failed {txt.name}: {e}')

        # Store in cache
        try:
            key = str(txt)
            mtime = txt.stat().st_mtime
            self._sidecar_cache[key] = (mtime, dict(data))
            self._sidecar_cache.move_to_end(key)
            while len(self._sidecar_cache) > self._sidecar_cache_max:
                self._sidecar_cache.popitem(last=False)
        except Exception:
            pass
        return data

    def _update_sidecar_kv(self, png_path: Path, key: str, value: str) -> None:
        txt = png_path.with_suffix('.txt')
        if not txt.exists():
            try:
                txt.write_text(f'{key}: {value}\n', encoding='utf-8')
            except Exception as e:
                self.log(f'[WARN] sidecar create failed: {e}')
            return
        try:
            lines = txt.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception as e:
            self.log(f'[WARN] sidecar read for update failed: {e}')
            lines = []
        out = []
        found = False
        prefix = f'{key}:'
        for ln in lines:
            if ln.startswith(prefix):
                out.append(f'{key}: {value}'.rstrip())
                found = True
            else:
                out.append(ln)
        if not found:
            out.append(f'{key}: {value}'.rstrip())
        try:
            txt.write_text('\n'.join(out) + '\n', encoding='utf-8')
        except Exception as e:
            self.log(f'[WARN] sidecar write failed: {e}')

    # ==================== Preview display ====================

    def _set_text_view(self, w: tk.Text, text: str) -> None:
        try:
            w.configure(state='normal')
            w.delete('1.0', 'end')
            w.insert('1.0', text or '')
            w.configure(state='disabled')
        except Exception:
            pass

    def _update_prompt_views_from_sidecar(self, meta: dict) -> None:
        raw_line = (meta.get('raw_line') or '')
        resolved = (meta.get('resolved') or '')
        # Append generation params if available
        params_parts = []
        for k in ('steps', 'guidance', 'width', 'height'):
            v = meta.get(k, '')
            if v:
                params_parts.append(f'{k}={v}')
        pipe = meta.get('pipe', '')
        mode = meta.get('mode', '')
        if pipe:
            params_parts.append(f'pipe={pipe}')
        if mode:
            params_parts.append(f'mode={mode}')
        params_line = '  '.join(params_parts)
        resolved_display = resolved
        if params_line:
            resolved_display = f'{resolved}\n── {params_line}'
        self._set_text_view(self.raw_view, raw_line)
        self._set_text_view(self.resolved_view, resolved_display)

    def _copy_to_clipboard(self, s: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(s)
            self.update()
        except Exception:
            pass

    def _copy_raw_clicked(self) -> None:
        try:
            s = self.raw_view.get('1.0', 'end-1c')
        except Exception:
            s = ''
        self._copy_to_clipboard(s)

    def _copy_resolved_clicked(self) -> None:
        try:
            s = self.resolved_view.get('1.0', 'end-1c')
        except Exception:
            s = ''
        self._copy_to_clipboard(s)

    def _show_current_image(self) -> None:
        p = self.browser.current()
        if not p:
            self._preview_canvas.delete('all')
            self._preview_tk_img = None
            self.preview_info.configure(text='-')
            self.title(f'{APP_NAME}  [0/{len(self.browser.files)}]')
            self._set_text_view(self.raw_view, '')
            self._set_text_view(self.resolved_view, '')
            try:
                self._update_inpaint_tab()
            except Exception:
                pass
            return

        try:
            im = Image.open(p).convert('RGB')
            iw, ih = im.size
            if iw <= 0 or ih <= 0:
                raise ValueError('invalid image size')

            if self._preview_zoom_fit.get():
                # Fit mode: scale to canvas size
                self._preview_canvas.update_idletasks()
                cw = max(200, self._preview_canvas.winfo_width())
                ch = max(200, self._preview_canvas.winfo_height())
                scale = min(cw / iw, ch / ih, 1.0)
                dw = max(1, int(iw * scale))
                dh = max(1, int(ih * scale))
                disp = im.resize((dw, dh), Image.LANCZOS) if scale < 1.0 else im
                tkimg = ImageTk.PhotoImage(disp)
                self._preview_canvas.delete('all')
                self._preview_canvas.create_image(cw // 2, ch // 2, anchor='center', image=tkimg)
                self._preview_canvas.configure(scrollregion=(0, 0, cw, ch))
            else:
                # 100% mode: original size with scroll
                tkimg = ImageTk.PhotoImage(im)
                self._preview_canvas.delete('all')
                self._preview_canvas.create_image(0, 0, anchor='nw', image=tkimg)
                self._preview_canvas.configure(scrollregion=(0, 0, iw, ih))

            self._preview_tk_img = tkimg  # prevent GC

            meta = self._read_sidecar(p)
            self._update_prompt_views_from_sidecar(meta)
            flag = (meta.get('flag') or '').strip()

            # Draw mark badge overlay on canvas
            badge_map = {'good': '✅', 'star': '⭐', 'bad': '❌'}
            if flag in badge_map:
                self._preview_canvas.create_text(
                    18, 18, text=badge_map[flag], font=('', 16),
                    fill='white', anchor='nw', tags='badge')

            mark_disp = f' [{flag}]' if flag else ''
            mask_badge = ' [ペイント済]' if str(p) in self._inpaint_damaged else ''
            zoom = 'fit' if self._preview_zoom_fit.get() else '100%'
            idx = self.browser.index + 1
            total = len(self.browser.files)
            total_all = getattr(self, '_total_png_count', total)
            filter_badge = f'/{total_all}' if total_all != total else ''
            self.preview_info.configure(text=f"{p.name}{mark_disp}{mask_badge}  {iw}x{ih}  [{zoom}]  ({idx}/{total}{filter_badge})")
            self.title(f'{APP_NAME}  [{idx}/{total}{filter_badge}]  {p.name}')
            # Update slider (with guard to prevent callback recursion)
            try:
                self._nav_slider_updating = True
                self._nav_slider.configure(to=max(1, total))
                self._nav_slider_var.set(idx)
            except Exception:
                pass
            finally:
                self._nav_slider_updating = False
        except Exception as e:
            self._set_text_view(self.raw_view, '')
            self._set_text_view(self.resolved_view, '')
            self.preview_info.configure(text=f'{p.name} (preview error: {e})')
            self.title(APP_NAME)

        # Update inpaint tab preview
        try:
            self._update_inpaint_tab()
        except Exception:
            pass

    def _toggle_preview_zoom(self) -> None:
        self._preview_zoom_fit.set(not self._preview_zoom_fit.get())
        self._show_current_image()

    def _prev_image(self) -> None:
        self.browser.prev()
        self._show_current_image()

    def _next_image(self) -> None:
        self.browser.next()
        self._show_current_image()

    def _first_image(self) -> None:
        if self.browser.files:
            self.browser.index = 0
            self._show_current_image()

    def _last_image(self) -> None:
        if self.browser.files:
            self.browser.index = len(self.browser.files) - 1
            self._show_current_image()

    def _on_nav_slider(self, value) -> None:
        """スライダーで画像位置をジャンプ"""
        if self._nav_slider_updating:
            return
        try:
            idx = int(float(value)) - 1  # 1-based → 0-based
            if 0 <= idx < len(self.browser.files) and idx != self.browser.index:
                self.browser.index = idx
                self._show_current_image()
        except Exception:
            pass

    # ==================== Marking & Export ====================

    def _mark_current_good(self) -> None:
        self._set_current_mark('good')

    def _mark_current_star(self) -> None:
        self._set_current_mark('star')

    def _mark_current_bad(self) -> None:
        self._set_current_mark('bad')

    def _clear_current_mark(self) -> None:
        self._set_current_mark('')

    def _set_current_mark(self, mark: str) -> None:
        p = self.browser.current()
        if not p or not Path(p).exists():
            return
        try:
            self._update_sidecar_kv(Path(p), 'flag', mark)
            txt = str(Path(p).with_suffix('.txt'))
            if txt in self._sidecar_cache:
                del self._sidecar_cache[txt]
            # Visual feedback: log + canvas flash
            mark_names = {'good': '✅ Good', 'star': '⭐ Star', 'bad': '❌ Bad', '': '🔄 解除'}
            flash_colors = {'good': '#1a4d1a', 'star': '#4d4d0a', 'bad': '#4d1a1a', '': '#1e1e1e'}
            self.log(f'{mark_names.get(mark, mark)}: {Path(p).name}')
            # Brief canvas flash
            color = flash_colors.get(mark, '#1e1e1e')
            self._preview_canvas.configure(bg=color)
            self.after(200, lambda: self._preview_canvas.configure(bg='#1e1e1e'))
        except Exception as e:
            self.log(f'[WARN] mark failed: {e}')
        self._apply_output_filter(keep_current=True)
        self._show_current_image()

    # P3: Unified export
    def _export_by_flag(self, flag: str) -> None:
        """Copy images with the given flag into outputs/_{flag}."""
        out_dir = self.paths.outputs_dir / f'_{flag}'
        _safe_mkdir(out_dir)
        count = 0
        try:
            for png in self.browser.files:
                meta = self._read_sidecar(Path(png))
                if (meta.get('flag') or '') == flag:
                    src = Path(png)
                    shutil.copy2(src, out_dir / src.name)
                    txt = src.with_suffix('.txt')
                    if txt.exists():
                        shutil.copy2(txt, out_dir / txt.name)
                    count += 1
        except Exception as e:
            messagebox.showerror(f'{flag} エクスポート', str(e))
            return
        messagebox.showinfo(f'{flag} エクスポート', f'{count}枚を {out_dir} にコピーしました')

    def _export_good_images(self) -> None:
        self._export_by_flag('good')

    def _export_star_images(self) -> None:
        self._export_by_flag('star')

    # ==================== Copy/append prompts ====================

    def _copy_fix_prompt_clicked(self) -> None:
        cur = self.browser.current()
        if not cur or not Path(cur).exists():
            return
        try:
            prompt, _, _ = self._repair_build_prompt(Path(cur))
        except Exception:
            prompt = ''
        self._copy_to_clipboard(prompt)

    def _append_raw_to_prompts_clicked(self) -> None:
        try:
            s = self.raw_view.get('1.0', 'end-1c').strip()
        except Exception:
            s = ''
        self._append_text_to_prompts(s)

    def _append_resolved_to_prompts_clicked(self) -> None:
        try:
            s = self.resolved_view.get('1.0', 'end-1c').strip()
        except Exception:
            s = ''
        self._append_text_to_prompts(s)

    def _append_text_to_prompts(self, s: str) -> None:
        if not s:
            return
        try:
            w = self.prompts_text
            cur = w.get('1.0', 'end-1c')
            if cur and not cur.endswith('\n'):
                w.insert('end', '\n')
            w.insert('end', s + '\n')
            w.see('end')
            w.focus_set()
        except Exception:
            pass

    def _open_outputs(self) -> None:
        try:
            p = str(self.paths.outputs_dir)
            if sys.platform.startswith('win'):
                os.startfile(p)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', p])
            else:
                subprocess.Popen(['xdg-open', p])
        except Exception as e:
            messagebox.showerror('出力フォルダ', str(e))

    def _open_current_in_viewer(self) -> None:
        """現在のプレビュー画像をOSの標準ビューアで開く"""
        cur = self.browser.current()
        if not cur or not cur.exists():
            return
        try:
            p = str(cur)
            if sys.platform.startswith('win'):
                os.startfile(p)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', p])
            else:
                subprocess.Popen(['xdg-open', p])
        except Exception as e:
            self.log(f'外部ビューア起動エラー: {e}')

    def _copy_current_path(self) -> None:
        """現在のプレビュー画像のファイルパスをクリップボードにコピー"""
        cur = self.browser.current()
        if not cur:
            return
        self._copy_to_clipboard(str(cur))

    def _set_current_as_ref(self) -> None:
        """現在のプレビュー画像を空いている参照スロットにセット"""
        cur = self.browser.current()
        if not cur or not cur.exists():
            return
        # Find first empty slot
        for i in range(MAX_REFS):
            if not self.ref_paths[i]:
                self.ref_paths[i] = str(cur)
                self._update_ref_ui()
                self._schedule_autosave()
                self.log(f'参照画像{i+1}にセット: {cur.name}')
                return
        # All slots full → ask to replace last
        if messagebox.askyesno('参照画像', f'全スロット使用中。スロット{MAX_REFS}を上書きしますか？'):
            self.ref_paths[MAX_REFS - 1] = str(cur)
            self._update_ref_ui()
            self._schedule_autosave()
            self.log(f'参照画像{MAX_REFS}に上書き: {cur.name}')

    def _send_current_to_nb(self) -> None:
        """現在のプレビュー画像をNanoBananaタブに送信（タブ切替 + Refineモード）"""
        cur = self.browser.current()
        if not cur or not cur.exists():
            return
        # Switch to NanoBanana tab
        try:
            self._left_nb.select(4)  # NanoBanana is tab index 4
        except Exception:
            pass
        # Set to refine mode
        self.nb_mode_var.set('refine')
        self._nb_mode_changed()
        self.log(f'🍌 NanoBananaに送信: {cur.name}')

    def _delete_current_image(self) -> None:
        """現在のプレビュー画像をゴミ箱に移動（_trash/ フォルダ）"""
        cur = self.browser.current()
        if not cur or not cur.exists():
            return
        if not messagebox.askyesno('削除', f'この画像を削除しますか？\n{cur.name}\n\n（_trash/ に移動されます）'):
            return
        try:
            trash_dir = self.paths.outputs_dir / '_trash'
            trash_dir.mkdir(exist_ok=True)
            dest = trash_dir / cur.name
            cur.rename(dest)
            # Also move sidecar if exists
            sidecar = cur.with_suffix('.txt')
            if sidecar.exists():
                sidecar.rename(trash_dir / sidecar.name)
            self.log(f'削除: {cur.name} → _trash/')
            self._rescan_outputs()
        except Exception as e:
            messagebox.showerror('削除エラー', str(e))

    # ==================== Generation ====================

    def _stop_clicked(self) -> None:
        self.stop_flag.set()
        self.log('停止を要求しました')

    def _generate_clicked(self) -> None:
        if self.is_generating:
            messagebox.showinfo('生成', '生成中です')
            return
        if self.engine.pipe is None:
            messagebox.showerror('生成', 'モデルが読み込まれていません')
            return

        raw_lines = self.prompts_text.get('1.0', 'end').splitlines()
        jobs = [ln.strip() for ln in raw_lines if ln.strip()]
        if not jobs:
            messagebox.showinfo('生成', 'プロンプトが入力されていません')
            return

        self.stop_flag.clear()
        self.is_generating = True
        samples = max(1, min(16, int(self.samples_var.get())))

        # Record prompts to history
        for j in jobs:
            if j not in self._prompt_history:
                self._prompt_history.insert(0, j)
        if len(self._prompt_history) > self._prompt_history_max:
            self._prompt_history = self._prompt_history[:self._prompt_history_max]
        self._prompt_history_count_var.set(f'({len(self._prompt_history)}件)')

        self._gen_btn.configure(text='⏳ 生成中...', state='disabled')
        self._open_progress(f'▶ 生成中 ({len(jobs)}行 x{samples}枚)')
        threading.Thread(target=self._generate_thread, args=(jobs,), daemon=True).start()

    def _resolve_prompt(self, raw_line: str, common: str) -> Tuple[str, str, List[int]]:
        combined = raw_line.strip()
        if common.strip():
            combined = (combined + ' ' + common.strip()).strip()

        ref_names = [v.get() for v in self.ref_name_vars]
        alias_pairs = PromptResolver.build_alias_map(ref_names)
        resolved = PromptResolver.substitute_names(combined, alias_pairs)

        used = PromptResolver.parse_used_image_indices(resolved)
        resolved_disp = PromptResolver.brackify_images(resolved)
        return resolved, resolved_disp, used

    def _get_common_prompt(self) -> str:
        base = (self.common_base_var.get() or '').strip()
        parts = []
        if base:
            parts.append(base)
        if self.common_story_on_var.get():
            s = (self.common_story_var.get() or '').strip()
            if s:
                parts.append(s)
        if self.common_strict_on_var.get():
            s = (self.common_strict_var.get() or '').strip()
            if s:
                parts.append(s)
        return ' '.join(parts).strip()

    def _save_sidecar(
        self,
        png_path: Path,
        raw_line: str,
        common: str,
        resolved: str,
        used_seed: int,
        pipe_name: str,
        ref_used: List[int],
        ref_desc: str,
        mode: str = 'generate',
        base_image: str = '',
        mask_image: str = '',
        mask_bbox: str = '',
    ) -> None:
        # Capture current generation parameters for reproducibility
        try:
            gen_params = {
                'steps': self.steps_var.get(),
                'guidance': self.guidance_var.get(),
                'width': self.w_var.get(),
                'height': self.h_var.get(),
                'model_path': self.model_path_var.get(),
                'cpu_offload': self.cpu_offload_var.get(),
            }
        except Exception:
            gen_params = {}

        lines = [
            f'mode: {mode}',
            f'base_image: {base_image}',
            f'mask_image: {mask_image}',
            f'mask_bbox: {mask_bbox}',
            f'raw_line: {raw_line}',
            f'common: {common}',
            f'resolved: {resolved}',
            f'used_seed: {used_seed}',
            f'flag: ',
            f'pipe: {pipe_name}',
            f'ref_passed: {ref_desc}',
            f'steps: {gen_params.get("steps", "")}',
            f'guidance: {gen_params.get("guidance", "")}',
            f'width: {gen_params.get("width", "")}',
            f'height: {gen_params.get("height", "")}',
            f'model_path: {gen_params.get("model_path", "")}',
        ]
        if ref_used:
            lines.append('refs:')
            for idx in ref_used:
                p = self.ref_paths[idx - 1] if 1 <= idx <= MAX_REFS else None
                nm = (self.ref_name_vars[idx - 1].get() or '').strip() if 1 <= idx <= MAX_REFS else ''
                if p:
                    lines.append(f'  image{idx}: file={Path(p).name}  name={nm}')
        else:
            lines.append('refs: none')
        try:
            png_path.with_suffix('.txt').write_text('\n'.join(lines), encoding='utf-8')
        except Exception as e:
            self._log_threadsafe(f'[WARN] sidecar save failed: {e}')

    # P0: Thread-safe generation thread
    def _generate_thread(self, jobs: List[str]) -> None:
        common = self._get_common_prompt()
        ref_total = sum(1 for p in self.ref_paths if p)

        try:
            samples = int(self.samples_var.get())
        except Exception:
            samples = 1
        samples = max(1, min(16, samples))

        self._log_threadsafe('--- 生成開始 ---')
        self._log_threadsafe(f'jobs: {len(jobs)}  ref_total: {ref_total}  samples_per_line: {samples}')

        gen_start = time.time()
        img_times = []  # per-image durations for ETA calc

        for i, raw_line in enumerate(jobs, start=1):
            if self.stop_flag.is_set():
                self._log_threadsafe('停止しました')
                break

            prompt, prompt_disp, used_refs = self._resolve_prompt(raw_line, common)

            ref_used = used_refs[:] if used_refs else []
            if ref_total and not ref_used:
                ref_used = [idx for idx in range(1, MAX_REFS + 1) if self.ref_paths[idx - 1]]

            ref_imgs = self._load_ref_images(ref_used) if ref_used else []

            self._log_threadsafe(f'[{i}/{len(jobs)}] prompt: {prompt_disp}')
            total_imgs = len(jobs) * samples
            done_base = (i - 1) * samples
            self.after(0, lambda ii=i, jl=len(jobs), s=samples:
                       self.pipe_label.configure(text=f'🔄 生成中 {ii}/{jl} x{s}'))
            if ref_total:
                self._log_threadsafe(f'ref: total={ref_total} used={ref_used if ref_used else "none"}')

            base_seed = -1
            if not self.random_seed_var.get():
                try:
                    base_seed = int(self.seed_var.get())
                except Exception:
                    base_seed = -1

            for v in range(samples):
                if self.stop_flag.is_set():
                    self._log_threadsafe('停止しました')
                    break

                seed = -1
                if not self.random_seed_var.get():
                    seed = base_seed
                    if seed >= 0 and samples > 1:
                        seed = seed + v

                try:
                    img, used_seed, ref_desc, dt = self.engine.generate_one(
                        prompt=prompt,
                        width=int(self.w_var.get()),
                        height=int(self.h_var.get()),
                        steps=int(self.steps_var.get()),
                        guidance=float(self.guidance_var.get()),
                        seed=seed,
                        ref_images=ref_imgs if ref_imgs else None,
                    )
                    # P0: UI updates via after()
                    self.after(0, lambda s=str(used_seed): self.used_seed_var.set(s))

                    self.job_counter += 1
                    self.after(0, self._schedule_autosave)

                    fname = (
                        f'{self.prefix_var.get().strip() or "story"}_'
                        f'{self.job_counter:04d}_p{i:04d}_v{v+1:02d}_'
                        f'{_now_stamp()}_s{used_seed}.png'
                    )
                    out_path = self.paths.outputs_dir / fname
                    img.save(out_path)

                    self._save_sidecar(
                        png_path=out_path,
                        raw_line=raw_line,
                        common=common,
                        resolved=prompt,
                        used_seed=used_seed,
                        pipe_name=self.engine.pipe_name,
                        ref_used=ref_used,
                        ref_desc=ref_desc,
                        mode='generate',
                    )

                    if ref_total:
                        self._log_threadsafe(f'ref: given={len(ref_imgs)} passed={ref_desc}')
                    self._log_threadsafe(f'[{i}/{len(jobs)}] v{v+1}/{samples} saved: {out_path.name} ({dt:.2f}s)')

                    # ETA calculation + progress update
                    img_times.append(dt)
                    done_count = done_base + v + 1
                    remaining = total_imgs - done_count
                    eta_str = ''
                    if img_times and remaining > 0:
                        avg_dt = sum(img_times) / len(img_times)
                        eta_sec = remaining * avg_dt
                        if eta_sec >= 60:
                            eta_str = f'残り約{eta_sec/60:.1f}分'
                        else:
                            eta_str = f'残り約{eta_sec:.0f}秒'
                    elapsed = time.time() - gen_start
                    self.after(0, lambda d=done_count, t=total_imgs, e=eta_str, el=elapsed:
                               (self.pipe_label.configure(
                                   text=f'🔄 {d}/{t}枚完了  {e}  (経過{el:.0f}s)'),
                                self._update_progress(d, t, e)))
                    # Show thumbnail of just-generated image
                    self.after(0, lambda im=img.copy(): self._update_progress_thumb(im))

                except Exception as e:
                    self._log_threadsafe(f'ERROR: {e}')
                    def _on_gen_err(m=str(e)):
                        self._close_progress()
                        self._gen_btn.configure(text='▶ 生成開始', state='normal')
                        messagebox.showerror('生成エラー', m)
                    self.after(0, _on_gen_err)
                    self.stop_flag.set()
                    break

            if self.stop_flag.is_set():
                break

        self.is_generating = False
        pipe_name = self.engine.pipe_name if self.engine.pipe else '-'
        def _on_gen_done(pn=pipe_name):
            self._close_progress()
            self.pipe_label.configure(text=f'モデル: {pn}')
            self._gen_btn.configure(text='▶ 生成開始', state='normal')
            self._rescan_outputs()  # single rescan after entire batch
        self.after(0, _on_gen_done)

    # ==================== Fix (repair) ====================

    def _repair_clicked(self) -> None:
        if self.is_generating:
            messagebox.showinfo('修正', '生成中です')
            return
        if self.engine.pipe is None:
            messagebox.showerror('修正', 'モデルが読み込まれていません')
            return
        cur = self.browser.current()
        if not cur or not cur.exists():
            messagebox.showerror('修正', 'プレビュー画像が選択されていません')
            return

        if not self._fix_skip_confirm_var.get():
            if not messagebox.askyesno('修正', 'プレビュー画像をベースに破綻を修正しますか？'):
                return

        self.stop_flag.clear()
        self.is_generating = True
        self._open_progress('🔧 修正中')
        threading.Thread(target=self._repair_thread, args=(cur,), daemon=True).start()

    def _collect_repair_images(self, base_png: Path) -> Tuple[List[Image.Image], List[int]]:
        base = Image.open(base_png).convert('RGB')
        bw, bh = base.size
        w, h = _fit_within_2048(bw, bh)
        if w > 0 and h > 0 and (bw != w or bh != h):
            base = base.resize((w, h), Image.LANCZOS)
        imgs: List[Image.Image] = [base]
        used_slots: List[int] = []
        for idx in range(1, MAX_REFS + 1):
            if len(imgs) >= REPAIR_MAX_TOTAL_IMAGES:
                break
            p = self.ref_paths[idx - 1]
            if not p:
                continue
            try:
                imgs.append(Image.open(p).convert('RGB'))
                used_slots.append(idx)
            except Exception as e:
                self._log_threadsafe(f'Ref load failed (image{idx}): {e}')
        return imgs, used_slots

    def _repair_build_prompt(self, base_png: Path) -> Tuple[str, str, int]:
        meta = self._read_sidecar(base_png)
        raw_line = (meta.get('raw_line') or '').strip()
        common = (meta.get('common') or '').strip()
        resolved = (meta.get('resolved') or '').strip()
        used_seed = meta.get('used_seed')

        if not resolved:
            resolved, _, _ = self._resolve_prompt(raw_line, common)

        shifted = PromptResolver.shift_image_indices(resolved, delta=1)

        fixed = (self.repair_prefix_var.get() or '').strip() if self.repair_prefix_use_var.get() else (
            'Use image1 as the base. Keep the same scene and background. '
            'Only fix artifacts like broken anatomy, hands, faces, clothes, and extra parts. '
            'Do not add new people.'
        )

        final_prompt = (fixed + ' ' + shifted).strip()
        disp = PromptResolver.brackify_images(final_prompt)

        final_seed = -1
        if self.use_original_seed_var.get() and isinstance(used_seed, int):
            final_seed = int(used_seed)
        return final_prompt, disp, final_seed

    # P0: Thread-safe repair thread
    def _repair_thread(self, base_png: Path) -> None:
        try:
            self._log_threadsafe('--- 修正開始 ---')

            prompt, prompt_disp, seed_from_meta = self._repair_build_prompt(base_png)
            imgs, used_slots = self._collect_repair_images(base_png)

            bw, bh = imgs[0].size
            width = _round_down_16(bw)
            height = _round_down_16(bh)

            self._log_threadsafe(f'base: {base_png.name}  size={bw}x{bh} -> {width}x{height}')
            self._log_threadsafe(f'prompt: {prompt_disp}')
            self._log_threadsafe(f'ref: base=image1 + slots={used_slots} (max 3)  total_images={len(imgs)}')

            steps = int(self.repair_steps_var.get())
            guidance = float(self.repair_guidance_var.get())

            seed = seed_from_meta
            if self.random_seed_var.get() and not self.use_original_seed_var.get():
                seed = -1

            img, used_seed, ref_desc, dt = self.engine.generate_one(
                prompt=prompt,
                width=width,
                height=height,
                steps=steps,
                guidance=guidance,
                seed=seed,
                ref_images=imgs,
            )
            self.after(0, lambda s=str(used_seed): self.used_seed_var.set(s))

            self.job_counter += 1
            self.after(0, self._schedule_autosave)

            fname = f'{self.prefix_var.get().strip() or "story"}_{self.job_counter:04d}_{_now_stamp()}_fix_s{used_seed}.png'
            out_path = self.paths.outputs_dir / fname
            img.save(out_path)

            self._save_sidecar(
                png_path=out_path,
                raw_line=f'FIX from {base_png.name}',
                common='',
                resolved=prompt,
                used_seed=used_seed,
                pipe_name=self.engine.pipe_name,
                ref_used=used_slots,
                ref_desc=ref_desc,
                mode='fix',
                base_image=base_png.name,
            )

            self._log_threadsafe(f'ref: passed={ref_desc}')
            self._log_threadsafe(f'saved: {out_path.name} ({dt:.2f}s)')
            self.after(0, self._rescan_outputs)

        except Exception as e:
            self._log_threadsafe(f'FIX ERROR: {e}')
            def _on_fix_err(m=str(e)):
                self._close_progress()
                messagebox.showerror('修正エラー', m)
            self.after(0, _on_fix_err)

        self.is_generating = False
        self.after(0, self._close_progress)

    # ==================== Mask editor ====================

    def _open_mask_editor_for_current(self) -> None:
        cur = self.browser.current()
        if not cur or not cur.exists():
            messagebox.showerror('ペインター', 'プレビュー画像が選択されていません')
            return
        self._open_mask_editor(cur)

    def _open_mask_editor(self, base_png: Path) -> None:
        """Open damage painter: paint directly on the image to mark areas for Klein to fix."""
        try:
            base_img = Image.open(base_png).convert('RGB')
        except Exception as e:
            messagebox.showerror('ペインター', f'画像を開けませんでした: {e}')
            return

        bw, bh = base_img.size
        scale = min(PREVIEW_MAX / bw, PREVIEW_MAX / bh, 1.0)
        vw = max(1, int(bw * scale))
        vh = max(1, int(bh * scale))
        base_view = base_img.resize((vw, vh), Image.LANCZOS)

        key = str(base_png)

        # Load existing or start fresh
        existing_dam = self._inpaint_damaged.get(key)
        existing_mask = self._inpaint_masks.get(key)
        if existing_dam is not None:
            try:
                work_img = existing_dam.resize((vw, vh), Image.LANCZOS).copy()
            except Exception:
                work_img = base_view.copy()
        else:
            work_img = base_view.copy()

        # Mask tracks exactly which pixels the user painted (white=painted)
        if existing_mask is not None:
            try:
                mask_view = existing_mask.resize((vw, vh), Image.NEAREST).copy()
            except Exception:
                mask_view = Image.new('L', (vw, vh), 0)
        else:
            mask_view = Image.new('L', (vw, vh), 0)

        top = tk.Toplevel(self)
        top.title(f'Damage Painter - {base_png.name}')
        top.geometry(f'{vw+240}x{max(vh+80, 560)}')

        left_frm = ttk.Frame(top)
        left_frm.pack(side='left', fill='both', expand=True, padx=8, pady=8)
        right_frm = ttk.Frame(top)
        right_frm.pack(side='right', fill='y', padx=8, pady=8)

        canvas = tk.Canvas(left_frm, width=vw, height=vh, bg='black', highlightthickness=0)
        canvas.pack()

        tk_img = ImageTk.PhotoImage(work_img)
        img_id = canvas.create_image(0, 0, anchor='nw', image=tk_img)
        canvas.image = tk_img

        brush_var = tk.IntVar(value=24)
        paint_color_var = tk.StringVar(value='red')
        color_map = {'white': (255,255,255), 'red': (255,0,0), 'green': (0,255,0), 'gray': (180,180,180)}

        undo_stack = []
        max_undo = 30

        def _push_undo():
            undo_stack.append((work_img.copy(), mask_view.copy()))
            if len(undo_stack) > max_undo:
                undo_stack.pop(0)

        def _undo(_ev=None):
            if not undo_stack:
                return
            w_restore, m_restore = undo_stack.pop()
            work_img.paste(w_restore)
            mask_view.paste(m_restore)
            _redraw()

        def _redraw():
            nonlocal tk_img
            tk_img = ImageTk.PhotoImage(work_img)
            canvas.itemconfigure(img_id, image=tk_img)
            canvas.image = tk_img

        drawing = {'on': False, 'last': None, 'mode': 'paint'}

        def _get_color():
            return color_map.get(paint_color_var.get(), (255, 0, 0))

        def _paint_at(x: int, y: int):
            r = max(1, int(brush_var.get()))
            bbox = (x - r, y - r, x + r, y + r)
            # Paint color on work image
            ImageDraw.Draw(work_img).ellipse(bbox, fill=_get_color())
            # Mark mask (white = painted area)
            ImageDraw.Draw(mask_view).ellipse(bbox, fill=255)

        def _restore_at(x: int, y: int):
            """Restore original pixels and clear mask"""
            r = max(1, int(brush_var.get()))
            sz = r * 2 + 1
            circle = Image.new('L', (sz, sz), 0)
            ImageDraw.Draw(circle).ellipse((0, 0, sz - 1, sz - 1), fill=255)
            px, py = x - r, y - r
            ox0 = max(0, px); oy0 = max(0, py)
            ox1 = min(vw, px + sz); oy1 = min(vh, py + sz)
            if ox0 >= ox1 or oy0 >= oy1:
                return
            mx0 = ox0 - px; my0 = oy0 - py
            mx1 = mx0 + (ox1 - ox0); my1 = my0 + (oy1 - oy0)
            # Restore original pixels
            orig_patch = base_view.crop((ox0, oy0, ox1, oy1))
            mask_patch = circle.crop((mx0, my0, mx1, my1))
            work_img.paste(orig_patch, (ox0, oy0), mask_patch)
            # Clear mask in this area
            black_patch = Image.new('L', (ox1 - ox0, oy1 - oy0), 0)
            mask_view.paste(black_patch, (ox0, oy0), mask_patch)

        def _on_down(ev):
            _push_undo()
            drawing['on'] = True
            drawing['mode'] = 'paint'
            drawing['last'] = (ev.x, ev.y)
            _paint_at(ev.x, ev.y)
            _redraw()

        def _on_move(ev):
            if not drawing['on']:
                return
            lx, ly = drawing['last'] or (ev.x, ev.y)
            drawing['last'] = (ev.x, ev.y)
            dx = ev.x - lx
            dy = ev.y - ly
            steps_count = max(1, int((dx*dx + dy*dy) ** 0.5 / max(1, brush_var.get() / 2)))
            for si in range(1, steps_count + 1):
                xi = int(lx + dx * (si / steps_count))
                yi = int(ly + dy * (si / steps_count))
                if drawing.get('mode') == 'erase':
                    _restore_at(xi, yi)
                else:
                    _paint_at(xi, yi)
            _redraw()

        def _on_up(ev):
            drawing['on'] = False
            drawing['last'] = None

        def _on_down_erase(ev):
            _push_undo()
            drawing['on'] = True
            drawing['mode'] = 'erase'
            drawing['last'] = (ev.x, ev.y)
            _restore_at(ev.x, ev.y)
            _redraw()

        canvas.bind('<ButtonPress-1>', _on_down)
        canvas.bind('<B1-Motion>', _on_move)
        canvas.bind('<ButtonRelease-1>', _on_up)
        canvas.bind('<ButtonPress-3>', _on_down_erase)
        canvas.bind('<B3-Motion>', _on_move)
        canvas.bind('<ButtonRelease-3>', _on_up)
        top.bind('<Control-z>', _undo)

        ttk.Label(right_frm, text='左クリック: 塗る（修復対象）\n右クリック: 元に戻す\nCtrl+Z: 取消',
                  wraplength=200).pack(anchor='w', pady=(0, 10))
        ttk.Label(right_frm, text='ペイント色').pack(anchor='w')
        for c in ['red', 'white', 'green', 'gray']:
            ttk.Radiobutton(right_frm, text=c, variable=paint_color_var, value=c).pack(anchor='w')

        ttk.Label(right_frm, text='ブラシサイズ').pack(anchor='w', pady=(10, 0))
        ttk.Scale(right_frm, from_=2, to=120, orient='horizontal', variable=brush_var).pack(fill='x', pady=(0, 8))

        def _reset():
            work_img.paste(base_view)
            mask_view.paste(0, (0, 0, vw, vh))
            _redraw()

        def _save_and_close():
            # Upscale both to full resolution
            # Damaged image: use LANCZOS for smooth image
            damaged_full = work_img.resize((bw, bh), Image.LANCZOS)
            # Mask: use NEAREST to keep crisp binary edges
            mask_full = mask_view.resize((bw, bh), Image.NEAREST)

            self._inpaint_damaged[key] = damaged_full
            self._inpaint_masks[key] = mask_full

            # Save to tmp for debugging
            try:
                self.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
                damaged_full.save(self.paths.tmp_dir / (base_png.stem + '_damaged.png'))
                mask_full.save(self.paths.tmp_dir / (base_png.stem + '_mask.png'))
            except Exception:
                pass
            self.after(0, self._show_current_image)
            top.destroy()

        ttk.Button(right_frm, text='リセット（元画像に戻す）', command=_reset).pack(fill='x', pady=(12, 6))
        ttk.Button(right_frm, text='保存して閉じる', command=_save_and_close).pack(fill='x', pady=(0, 6))
        ttk.Button(right_frm, text='キャンセル', command=top.destroy).pack(fill='x')

    # ==================== Inpaint ====================

    def _inpaint_clicked(self) -> None:
        if self.is_generating:
            messagebox.showinfo('修復', '生成中です')
            return
        if self.engine.pipe is None:
            messagebox.showerror('修復', 'モデルが読み込まれていません')
            return
        cur = self.browser.current()
        if not cur or not cur.exists():
            messagebox.showerror('修復', 'プレビュー画像が選択されていません')
            return

        key = str(cur)
        if key not in self._inpaint_damaged:
            if messagebox.askyesno('修復',
                    'まだペイントされていません。\n'
                    'ペインターを開いて修復したい部分を塗りますか？'):
                self._open_mask_editor(cur)
            return

        if not messagebox.askyesno('修復', 'ペイント済み画像をKleinに送って修復しますか？'):
            return

        self.stop_flag.clear()
        self.is_generating = True
        self._open_progress('🎨 修復中')
        threading.Thread(target=self._inpaint_thread, args=(cur,), daemon=True).start()

    def _inpaint_build_prompt(self, base_png: Path) -> Tuple[str, str, int]:
        meta = self._read_sidecar(base_png)
        raw_line = (meta.get('raw_line') or '').strip()
        common = (meta.get('common') or '').strip()
        resolved = (meta.get('resolved') or '').strip()
        used_seed = meta.get('used_seed')

        if not resolved:
            resolved, _, _ = self._resolve_prompt(raw_line, common)

        shifted = PromptResolver.shift_image_indices(resolved, delta=1)

        override = (self.inpaint_override_var.get() or '').strip()
        style_tail = ''
        if resolved and ',' in resolved:
            try:
                style_tail = resolved.split(',', 1)[1].strip()
            except Exception:
                style_tail = ''

        fixed = (self.inpaint_prefix_var.get() or '').strip() if self.inpaint_prefix_use_var.get() else (
            'Reproduce image1 faithfully. '
            'The image has some painted-over / whited-out / damaged areas. '
            'Fix those areas to look natural, matching the surrounding art style, '
            'colors, and details. Keep everything else exactly as-is.'
        )

        if override:
            body = override
            if style_tail:
                body = (body + ', ' + style_tail).strip()
            body = PromptResolver.shift_image_indices(body, delta=1)
        else:
            body = shifted

        final_prompt = (fixed + ' ' + body).strip()
        disp = PromptResolver.brackify_images(final_prompt)

        final_seed = -1
        if self.use_original_seed_var.get() and isinstance(used_seed, int):
            final_seed = int(used_seed)
        return final_prompt, disp, final_seed

    # P0: Thread-safe inpaint thread
    def _inpaint_thread(self, base_png: Path) -> None:
        try:
            self._log_threadsafe('--- Inpaint (damage → Klein → mask composite) ---')

            key = str(base_png)
            damaged_full = self._inpaint_damaged.get(key)
            if damaged_full is None:
                raise RuntimeError('No damaged image. Use the Damage Painter first.')

            prompt, prompt_disp, seed_from_meta = self._inpaint_build_prompt(base_png)

            imgs, used_slots = self._collect_repair_images(base_png)
            base = imgs[0]
            bw, bh = base.size

            # Ensure damaged image matches base size
            if damaged_full.size != (bw, bh):
                damaged_full = damaged_full.resize((bw, bh), Image.LANCZOS)

            # ---- Read settings ----
            mode = self.inpaint_mode_var.get() or 'full'
            try:
                mask_grow = max(0, int(self.inpaint_mask_grow_var.get()))
            except Exception:
                mask_grow = 4
            try:
                mask_blur = max(0, int(self.inpaint_mask_blur_var.get()))
            except Exception:
                mask_blur = 8
            try:
                damage_blur = max(0, int(self.inpaint_damage_blur_var.get()))
            except Exception:
                damage_blur = 0

            steps = int(self.repair_steps_var.get())
            guidance = float(self.repair_guidance_var.get())
            seed = seed_from_meta
            if self.random_seed_var.get() and not self.use_original_seed_var.get():
                seed = -1

            # ---- Use the explicit mask recorded during painting ----
            paint_mask = self._inpaint_masks.get(key)
            if paint_mask is None:
                raise RuntimeError('No mask found. Re-open Damage Painter and save.')
            if paint_mask.size != (bw, bh):
                paint_mask = paint_mask.resize((bw, bh), Image.NEAREST)

            mb = paint_mask.getbbox()
            if mb is None:
                raise RuntimeError('Mask is empty (no painted area detected)')

            # ---- Blur damage edges before sending to Klein (optional) ----
            klein_input = damaged_full
            if damage_blur > 0:
                soft_edge = paint_mask.filter(ImageFilter.GaussianBlur(radius=damage_blur))
                klein_input = Image.composite(damaged_full, base, soft_edge)
                self._log_threadsafe(f'damage_blur={damage_blur}px applied to Klein input')

            # ---- Build composite mask: grow + blur for soft edges ----
            composite_mask = paint_mask.copy()
            if mask_grow > 0:
                kernel = min(mask_grow * 2 + 1, 31)
                try:
                    composite_mask = composite_mask.filter(ImageFilter.MaxFilter(kernel))
                except Exception:
                    for _ in range(mask_grow):
                        composite_mask = composite_mask.filter(ImageFilter.MaxFilter(3))
            if mask_blur > 0:
                composite_mask = composite_mask.filter(ImageFilter.GaussianBlur(radius=mask_blur))

            self._log_threadsafe(f'paint_mask bbox: {mb}  grow={mask_grow}  blur={mask_blur}')

            if mode == 'crop':
                # ---- Crop mode ----
                x0, y0, x1, y1 = mb
                try:
                    pad_pct = float(self.inpaint_pad_pct_var.get())
                except Exception:
                    pad_pct = 0.15
                try:
                    min_crop = int(self.inpaint_min_crop_var.get())
                except Exception:
                    min_crop = 512

                pad = int(max(64, min(bw, bh) * max(0.0, pad_pct)))
                x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
                x1 = min(bw, x1 + pad); y1 = min(bh, y1 + pad)

                cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
                need = max(min_crop, x1 - x0, y1 - y0)
                half = need // 2
                x0 = max(0, cx - half); y0 = max(0, cy - half)
                x1 = min(bw, cx + half); y1 = min(bh, cy + half)

                damaged_crop = klein_input.crop((x0, y0, x1, y1))
                mask_crop = composite_mask.crop((x0, y0, x1, y1))
                orig_crop = base.crop((x0, y0, x1, y1))
                cw, ch = damaged_crop.size
                cw2 = max(16, _round_down_16(cw))
                ch2 = max(16, _round_down_16(ch))
                if (cw2, ch2) != (cw, ch):
                    damaged_crop = damaged_crop.resize((cw2, ch2), Image.LANCZOS)

                self._log_threadsafe(f'[crop] region=({x0},{y0})-({x1},{y1})  gen_size={cw2}x{ch2}')
                self._log_threadsafe(f'prompt: {prompt_disp}')

                ref_imgs = [damaged_crop] + (imgs[1:4] if self.inpaint_use_full_ref_var.get() else [])

                gen_crop, used_seed, ref_desc, dt = self.engine.generate_one(
                    prompt=prompt, width=cw2, height=ch2,
                    steps=steps, guidance=guidance, seed=seed,
                    ref_images=ref_imgs,
                )
                self.after(0, lambda s=str(used_seed): self.used_seed_var.set(s))

                # Resize gen_crop back to paste size
                paste_w, paste_h = x1 - x0, y1 - y0
                if gen_crop.size != (paste_w, paste_h):
                    gen_crop = gen_crop.resize((paste_w, paste_h), Image.LANCZOS)
                if mask_crop.size != (paste_w, paste_h):
                    mask_crop = mask_crop.resize((paste_w, paste_h), Image.LANCZOS)

                # Mask composite: Klein output in damaged area, original elsewhere
                composited_crop = Image.composite(gen_crop, orig_crop, mask_crop)
                out_img = base.copy()
                out_img.paste(composited_crop, (x0, y0))
                self._log_threadsafe(f'[crop] mask-composited at ({x0},{y0})')

            else:
                # ---- Full mode ----
                gen_w = _round_down_16(bw)
                gen_h = _round_down_16(bh)
                klein_resized = klein_input
                if (gen_w, gen_h) != (bw, bh):
                    klein_resized = klein_input.resize((gen_w, gen_h), Image.LANCZOS)

                self._log_threadsafe(f'[full] gen_size={gen_w}x{gen_h}')
                self._log_threadsafe(f'prompt: {prompt_disp}')

                ref_imgs = [klein_resized] + (imgs[1:4] if self.inpaint_use_full_ref_var.get() else [])

                gen_img, used_seed, ref_desc, dt = self.engine.generate_one(
                    prompt=prompt, width=gen_w, height=gen_h,
                    steps=steps, guidance=guidance, seed=seed,
                    ref_images=ref_imgs,
                )
                self.after(0, lambda s=str(used_seed): self.used_seed_var.set(s))

                # Resize back to original if needed
                if gen_img.size != (bw, bh):
                    gen_img = gen_img.resize((bw, bh), Image.LANCZOS)

                # Mask composite: Klein output in damaged area, original elsewhere
                out_img = Image.composite(gen_img, base, composite_mask)
                self._log_threadsafe(f'[full] mask-composited (damaged area from Klein, rest from original)')

            self.job_counter += 1
            self.after(0, self._schedule_autosave)

            fname = f'{self.prefix_var.get().strip() or "story"}_{self.job_counter:04d}_{_now_stamp()}_inpaint_s{used_seed}.png'
            out_path = self.paths.outputs_dir / fname
            out_img.save(out_path)

            self._save_sidecar(
                png_path=out_path,
                raw_line=f'INPAINT from {base_png.name}',
                common='',
                resolved=prompt,
                used_seed=used_seed,
                pipe_name=self.engine.pipe_name,
                ref_used=used_slots,
                ref_desc=ref_desc,
                mode=f'inpaint-{mode}',
                base_image=base_png.name,
            )

            self._log_threadsafe(f'saved: {out_path.name} ({dt:.2f}s) grow={mask_grow} blur={mask_blur}')
            self.after(0, self._rescan_outputs)

        except Exception as e:
            self._log_threadsafe(f'INPAINT ERROR: {e}')
            def _on_inp_err(m=str(e)):
                self._close_progress()
                messagebox.showerror('修復エラー', m)
            self.after(0, _on_inp_err)

        self.is_generating = False
        self.after(0, self._close_progress)

    @staticmethod
    def _match_border_color(
        original_crop: Image.Image,
        generated_crop: Image.Image,
        mask: Image.Image,
        margin: int = 24,
    ) -> Image.Image:
        """Adjust generated crop's color/brightness to match original at the
        mask boundary, preventing visible color shift at composite edges.

        Uses a simple linear histogram transfer on the border band pixels.
        """
        import numpy as np
        w, h = original_crop.size
        orig = np.array(original_crop.convert('RGB'), dtype=np.float32)
        gen  = np.array(generated_crop.convert('RGB'), dtype=np.float32)
        m    = np.array(mask.convert('L'), dtype=np.float32) / 255.0

        # Build border band: pixels near mask edge (dilated mask - eroded mask)
        from PIL import ImageFilter
        mask_pil = mask.convert('L')
        dilated = mask_pil.filter(ImageFilter.MaxFilter(margin * 2 + 1))
        eroded  = mask_pil.filter(ImageFilter.MinFilter(max(3, margin)))
        band    = np.array(dilated, dtype=np.float32) / 255.0 - np.array(eroded, dtype=np.float32) / 255.0
        band    = np.clip(band, 0, 1)

        # We need some border pixels to compute stats
        border_mask = band > 0.3
        if border_mask.sum() < 10:
            return generated_crop  # too few pixels, skip

        for ch in range(3):
            orig_vals = orig[:, :, ch][border_mask]
            gen_vals  = gen[:, :, ch][border_mask]
            if gen_vals.std() < 1e-6:
                continue
            # Linear transfer: shift mean and scale std
            o_mean, o_std = orig_vals.mean(), max(orig_vals.std(), 1e-6)
            g_mean, g_std = gen_vals.mean(), max(gen_vals.std(), 1e-6)
            # Apply to entire image (inside mask area)
            gen[:, :, ch] = (gen[:, :, ch] - g_mean) * (o_std / g_std) + o_mean

        gen = np.clip(gen, 0, 255).astype(np.uint8)
        return Image.fromarray(gen)

    # ==================== P4: Settings persistence (declarative) ====================

    def _default_settings_path(self) -> Path:
        try:
            return self.paths.script_dir / SETTINGS_FILENAME
        except Exception:
            return Path(__file__).resolve().parent / SETTINGS_FILENAME

    def _last_settings_pointer_path(self) -> Path:
        try:
            return self.paths.script_dir / 'zunkey_klein_story_settings_last.txt'
        except Exception:
            return Path(__file__).resolve().parent / 'zunkey_klein_story_settings_last.txt'

    def _write_last_settings_path(self, p: Path) -> None:
        try:
            lp = self._last_settings_pointer_path()
            lp.write_text(str(p), encoding='utf-8')
        except Exception as e:
            self.log(f'[WARN] write last settings path failed: {e}')

    def _settings_path(self) -> Path:
        try:
            s = (self.settings_path_var.get() or '').strip()
            if s:
                p = Path(s)
                if p.suffix.lower() != '.json':
                    p = p.with_suffix('.json')
                return p
        except Exception:
            pass
        return self._default_settings_path()

    def _collect_settings(self) -> dict:
        """Collect all settings into a dict (declarative + special fields)."""
        # Sync NanoBanana prompt text to var before saving
        try:
            self._nb_save_current_prompt()
        except Exception:
            pass

        data = {
            'app': APP_NAME,
            'version': 1,
            'saved_at': _now_stamp(),
        }

        # Window geometry (special)
        try:
            data['window_geometry'] = self.geometry()
        except Exception:
            data['window_geometry'] = ''

        # Declarative fields
        for json_key, attr, typ, default in self._SETTINGS_MAP:
            try:
                data[json_key] = typ(getattr(self, attr).get())
            except Exception:
                data[json_key] = default

        # Special: prompts text
        try:
            data['prompts_text'] = self.prompts_text.get('1.0', 'end-1c')
        except Exception:
            data['prompts_text'] = ''

        # Special: refs
        refs = []
        for i in range(MAX_REFS):
            refs.append({
                'path': self.ref_paths[i],
                'name': (self.ref_name_vars[i].get() or '').strip(),
            })
        data['refs'] = refs

        # Special: job counter
        data['job_counter'] = int(self.job_counter)

        # Special: prompt history
        data['prompt_history'] = self._prompt_history[:self._prompt_history_max]

        return data

    def _apply_settings(self, data: dict) -> None:
        """Apply settings from a dict (declarative + special fields)."""
        if not isinstance(data, dict):
            return

        self._loading_settings = True
        try:
            # Window geometry
            geo = (data.get('window_geometry') or '').strip()
            if geo:
                try:
                    self.geometry(geo)
                except Exception:
                    pass

            # Declarative fields
            for json_key, attr, typ, default in self._SETTINGS_MAP:
                if json_key in data:
                    try:
                        getattr(self, attr).set(typ(data[json_key]))
                    except Exception:
                        pass

            # Backward compat: old single common_prompt -> Base
            if 'common_prompt' in data and 'common_base' not in data:
                self.common_base_var.set(str(data.get('common_prompt') or ''))
                self.common_story_var.set('')
                self.common_strict_var.set('')
                self.common_story_on_var.set(False)
                self.common_strict_on_var.set(False)

            # Prompts text
            if 'prompts_text' in data:
                try:
                    self.prompts_text.delete('1.0', 'end')
                    self.prompts_text.insert('1.0', str(data.get('prompts_text') or ''))
                except Exception:
                    pass

            # Refs
            if 'refs' in data and isinstance(data.get('refs'), list):
                refs = data['refs'][:MAX_REFS]
                for i in range(MAX_REFS):
                    if i < len(refs) and isinstance(refs[i], dict):
                        self.ref_paths[i] = refs[i].get('path')
                        self.ref_name_vars[i].set(str(refs[i].get('name') or ''))
                    else:
                        self.ref_paths[i] = None
                        self.ref_name_vars[i].set('')
                try:
                    self._update_ref_ui()
                except Exception:
                    pass

            # Job counter
            if 'job_counter' in data:
                try:
                    self.job_counter = int(data.get('job_counter') or 0)
                except Exception:
                    self.job_counter = 0

            try:
                self._on_random_seed_toggle()
            except Exception:
                pass

            # Restore NanoBanana prompt text for current mode
            try:
                self._nb_mode_changed()
            except Exception:
                pass

            # Restore prompt history
            try:
                hist = data.get('prompt_history')
                if isinstance(hist, list):
                    self._prompt_history = [str(h) for h in hist[:self._prompt_history_max]]
                    self._prompt_history_count_var.set(f'({len(self._prompt_history)}件)')
            except Exception:
                pass

        finally:
            self._loading_settings = False

    def _save_settings(self, silent: bool = False, path: Optional[Path] = None) -> bool:
        try:
            p = path if path is not None else self._settings_path()
            if path is not None:
                try:
                    self.settings_path_var.set(str(p))
                except Exception:
                    pass

            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            data = self._collect_settings()

            # Backup existing settings before overwrite
            if p.exists():
                try:
                    bak = p.with_suffix('.json.bak')
                    shutil.copy2(p, bak)
                except Exception:
                    pass

            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            self._write_last_settings_path(p)
            if not silent:
                self.log(f'設定を保存: {p.name}')
            return True
        except Exception as e:
            if not silent:
                self.log(f'設定の保存に失敗: {e}')
            return False

    def _load_settings(self, silent: bool = False, path: Optional[Path] = None) -> bool:
        try:
            p = path if path is not None else self._settings_path()
            if path is not None:
                try:
                    self.settings_path_var.set(str(p))
                except Exception:
                    pass

            if not p.exists():
                return False

            data = json.loads(p.read_text(encoding='utf-8'))
            self._apply_settings(data)
            self._write_last_settings_path(p)
            if not silent:
                self.log(f'設定を読み込み: {p.name}')
            return True
        except Exception as e:
            if not silent:
                self.log(f'設定の読み込み失敗: {e}')
            return False

    def _schedule_autosave(self, delay_ms: int = 800) -> None:
        if self._loading_settings:
            return
        try:
            if self._save_settings_after_id is not None:
                try:
                    self.after_cancel(self._save_settings_after_id)
                except Exception:
                    pass
                self._save_settings_after_id = None

            self._save_settings_after_id = self.after(delay_ms, lambda: self._save_settings(silent=True))
        except Exception:
            pass

    # backward compatible alias
    def _schedule_save_settings(self) -> None:
        self._schedule_autosave()

    def _bind_autosave_traces(self) -> None:
        # Collect all tk variables that should trigger autosave
        vars_to_trace = []
        for _, attr, _, _ in self._SETTINGS_MAP:
            try:
                vars_to_trace.append(getattr(self, attr))
            except AttributeError:
                pass
        for v in self.ref_name_vars:
            vars_to_trace.append(v)

        for v in vars_to_trace:
            try:
                v.trace_add('write', lambda *args: self._schedule_autosave())
            except Exception:
                pass

        # Text widgets
        try:
            self.prompts_text.bind('<KeyRelease>', lambda e: self._schedule_autosave())
            self.prompts_text.bind('<FocusOut>', lambda e: self._schedule_autosave())
        except Exception:
            pass

    # ==================== Settings UI callbacks ====================

    def _save_settings_clicked(self) -> None:
        p = self._settings_path()
        ok = self._save_settings(silent=True, path=p)
        if ok:
            messagebox.showinfo('設定', f'保存しました:\n{p.name}')
        else:
            messagebox.showerror('設定', '保存に失敗しました')

    def _save_settings_as_clicked(self) -> None:
        try:
            cur = self._settings_path()
            initialfile = cur.name
        except Exception:
            initialfile = 'settings.json'
        fname = filedialog.asksaveasfilename(
            title='設定を別名で保存',
            defaultextension='.json',
            filetypes=[('JSON', '*.json')],
            initialdir=str(self.paths.script_dir),
            initialfile=initialfile
        )
        if not fname:
            return
        p = Path(fname)
        ok = self._save_settings(silent=True, path=p)
        if ok:
            messagebox.showinfo('設定', f'保存しました:\n{p.name}')
        else:
            messagebox.showerror('設定', '保存に失敗しました')

    def _load_settings_clicked(self) -> None:
        fname = filedialog.askopenfilename(
            title='設定の読み込み',
            filetypes=[('JSON', '*.json')],
            initialdir=str(self.paths.script_dir)
        )
        if not fname:
            return
        p = Path(fname)
        ok = self._load_settings(silent=True, path=p)
        if ok:
            messagebox.showinfo('設定', f'読み込みました:\n{p.name}')
            try:
                self._update_ref_ui()
            except Exception:
                pass
        else:
            messagebox.showwarning('設定', '読み込みに失敗しました')

    def _on_close(self) -> None:
        # Stop any running generation
        if self.is_generating:
            self.stop_flag.set()
            self._close_progress()
            self.log('終了前に生成を停止しました')
            # Give thread a moment to notice the stop flag
            self.update_idletasks()
        self._save_settings(silent=True)
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass

    # ==================== Presets ====================

    def _sanitize_preset_name(self, name: str) -> str:
        name = (name or '').strip()
        if not name:
            return ''
        name = re.sub(r'[\\/:*?"<>|]+', '_', name)
        name = name.strip().strip('.')
        return name

    def _preset_path_for_name(self, name: str) -> Optional[Path]:
        n = self._sanitize_preset_name(name)
        if not n:
            return None
        return Path(self.presets_dir) / f'{n}.json'

    def _refresh_presets(self) -> None:
        try:
            _safe_mkdir(Path(self.presets_dir))
            files = sorted(Path(self.presets_dir).glob('*.json'))
            names = [f.stem for f in files]
        except Exception:
            names = []
        # Rebuild dynamic menu entries (after separator at index 2)
        try:
            menu = self._presets_menu
            while menu.index('end') is not None and menu.index('end') >= 3:
                menu.delete(3)
            for name in names:
                menu.add_command(label=name,
                                 command=lambda n=name: self._load_preset_by_name(n))
        except Exception:
            pass

    def _load_preset_by_name(self, name: str) -> None:
        p = self._preset_path_for_name(name)
        if p is None or not p.exists():
            messagebox.showerror('プリセット', f'見つかりません: {name}')
            return
        try:
            ok = self._load_settings(silent=True, path=p)
            if not ok:
                raise RuntimeError('load failed')
            self.settings_path_var.set(str(p))
            self._write_last_settings_path(p)
            self.log(f'プリセット読込: {name}')
        except Exception as e:
            messagebox.showerror('プリセット', str(e))

    def _on_preset_combo_selected(self, event=None) -> None:
        pass  # No longer used (menu-based presets)

    def _save_preset_clicked(self) -> None:
        try:
            from tkinter import simpledialog
            raw = simpledialog.askstring('Save Preset', 'Preset name:', parent=self)
        except Exception:
            raw = None
        name = self._sanitize_preset_name(raw or '')
        if not name:
            return
        p = self._preset_path_for_name(name)
        if p is None:
            messagebox.showerror('プリセット', '無効な名前です')
            return
        try:
            _safe_mkdir(p.parent)
            ok = self._save_settings(silent=True, path=p)
            if not ok:
                raise RuntimeError('save failed')
            self.settings_path_var.set(str(p))
            self._write_last_settings_path(p)
            self._refresh_presets()
            messagebox.showinfo('プリセット', f'保存しました: {name}')
        except Exception as e:
            messagebox.showerror('プリセット', str(e))

    def _load_preset_clicked(self) -> None:
        # Presets are loaded via menu; this is a fallback
        pass

    def _delete_preset_clicked(self) -> None:
        try:
            names = sorted(f.stem for f in Path(self.presets_dir).glob('*.json'))
        except Exception:
            names = []
        if not names:
            messagebox.showinfo('プリセット', 'プリセットがありません')
            return
        try:
            from tkinter import simpledialog
            name = simpledialog.askstring('Delete Preset',
                                           f'Name to delete:\n{", ".join(names)}', parent=self)
        except Exception:
            name = None
        name = self._sanitize_preset_name(name or '')
        if not name:
            return
        p = self._preset_path_for_name(name)
        if p is None or not p.exists():
            messagebox.showerror('プリセット', f'見つかりません: {name}')
            return
        if not messagebox.askyesno('プリセット', f'プリセット「{name}」を削除しますか？'):
            return
        try:
            p.unlink()
            self._refresh_presets()
            self.log(f'プリセット削除: {name}')
        except Exception as e:
            messagebox.showerror('プリセット', str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = ZunkeyKleinStoryApp()
    app.mainloop()


if __name__ == '__main__':
    main()
