"""Install MiVOLO's model package (https://github.com/WildChlamydia/MiVOLO, Apache-2.0).

The Hugging Face model `iitolstykh/mivolo_v2` imports the `mivolo` package, which is not on PyPI.
Its setup.py pins timm 0.8 and ultralytics, which this tool does not need and which conflict with
current timm. So only the `mivolo/` package is copied from a pinned commit, and the few places
that use timm APIs renamed since 0.9 are patched.
"""

from __future__ import annotations

import io
import shutil
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
from pathlib import Path

MIVOLO_REF = "37475e3f8818b5f22448003feec3e64b01bfb188"
_URL = "https://github.com/WildChlamydia/MiVOLO/archive/{ref}.tar.gz"

# (file inside mivolo/, original text, replacement). Each must match exactly once.
PATCHES: list[tuple[str, str, str]] = [
    (
        "model/create_timm_model.py",
        "from timm.models._helpers import load_state_dict, remap_checkpoint\n",
        "from timm.models._helpers import load_state_dict\n"
        "try:\n"
        "    from timm.models._helpers import remap_checkpoint\n"
        "except ImportError:  # renamed in timm 0.9, with the arguments swapped\n"
        "    from timm.models._helpers import remap_state_dict as _remap_state_dict\n"
        "\n"
        "    def remap_checkpoint(model, state_dict, allow_reshape=True):\n"
        "        return _remap_state_dict(state_dict, model, allow_reshape)\n",
    ),
    (
        "model/create_timm_model.py",
        "from timm.models._pretrained import PretrainedCfg, split_model_name_tag\n",
        "from timm.models._pretrained import PretrainedCfg\n"
        "try:\n"
        "    from timm.models._pretrained import split_model_name_tag\n"
        "except ImportError:  # moved in timm 0.9\n"
        "    from timm.models._registry import split_model_name_tag\n",
    ),
    (
        # timm 0.9 added `pos_drop_rate` after `drop_rate` in VOLO.__init__, shifting MiVOLO's
        # positional arguments; pass the ones after `drop_rate` by name (valid for old and new timm).
        "model/mivolo_model.py",
        "            drop_rate,\n"
        "            attn_drop_rate,\n"
        "            drop_path_rate,\n"
        "            norm_layer,\n"
        "            post_layers,\n"
        "            use_aux_head,\n"
        "            use_mix_token,\n"
        "            pooling_scale,\n"
        "        )\n",
        "            drop_rate,\n"
        "            attn_drop_rate=attn_drop_rate,\n"
        "            drop_path_rate=drop_path_rate,\n"
        "            norm_layer=norm_layer,\n"
        "            post_layers=post_layers,\n"
        "            use_aux_head=use_aux_head,\n"
        "            use_mix_token=use_mix_token,\n"
        "            pooling_scale=pooling_scale,\n"
        "        )\n",
    ),
    (
        "model/create_timm_model.py",
        "pretrained_cfg, model_name = load_model_config_from_hf(model_name)\n",
        "pretrained_cfg, model_name = load_model_config_from_hf(model_name)[:2]  # 3 values in timm 1.0\n",
    ),
]


def apply_patches(package_dir: Path) -> None:
    for rel, old, new in PATCHES:
        path = package_dir / rel
        text = path.read_text()
        if text.count(old) != 1:
            raise RuntimeError(f"MiVOLO patch for {rel} no longer applies (expected once: {old.strip()!r})")
        path.write_text(text.replace(old, new))


def install(target: Path | None = None, ref: str = MIVOLO_REF, *, archive: bytes | None = None) -> Path:
    """Download MiVOLO at `ref`, patch it and copy the `mivolo` package into `target` (site-packages)."""
    target = target or Path(sysconfig.get_paths()["purelib"])
    if archive is None:
        with urllib.request.urlopen(_URL.format(ref=ref), timeout=120) as resp:
            archive = resp.read()
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = [m for m in tar.getmembers() if "/mivolo/" in f"/{m.name}" and m.isreg()]
            for m in members:
                # Archive layout: MiVOLO-<ref>/mivolo/...; keep everything from "mivolo/" on.
                rel = m.name[m.name.index("mivolo/"):]
                dest = Path(tmp) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src:  # type: ignore[union-attr]
                    dest.write_bytes(src.read())
        package = Path(tmp) / "mivolo"
        if not (package / "model" / "create_timm_model.py").exists():
            raise RuntimeError("The MiVOLO archive does not contain the expected package.")
        apply_patches(package)
        dest = target / "mivolo"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(package, dest)
    return dest


def main() -> None:
    dest = install()
    print(f"Installed MiVOLO {MIVOLO_REF[:7]} to {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
