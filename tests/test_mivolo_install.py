import io
import tarfile

import pytest

from immich_metadatenmaster.estimators.mivolo_install import PATCHES, install

ORIGINAL = """from timm.models._helpers import load_state_dict, remap_checkpoint
from timm.models._pretrained import PretrainedCfg, split_model_name_tag

def create_model(model_name):
    pretrained_cfg, model_name = load_model_config_from_hf(model_name)
"""


MODEL = """class MiVOLOModel(VOLO):
    def __init__(self, layers, drop_rate=0.0, attn_drop_rate=0.0, drop_path_rate=0.0, norm_layer=None,
                 post_layers=None, use_aux_head=True, use_mix_token=False, pooling_scale=2):
        super().__init__(
            layers,
            drop_rate,
            attn_drop_rate,
            drop_path_rate,
            norm_layer,
            post_layers,
            use_aux_head,
            use_mix_token,
            pooling_scale,
        )
"""


def archive(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"MiVOLO-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_installs_only_the_package_and_patches_it(tmp_path):
    data = archive({"mivolo/__init__.py": "", "mivolo/model/create_timm_model.py": ORIGINAL,
                    "mivolo/model/mivolo_model.py": MODEL,
                    "setup.py": "import pkg_resources", "demo.py": "x"})
    dest = install(tmp_path, archive=data)
    assert dest == tmp_path / "mivolo"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mivolo"]
    for rel, _, new in PATCHES:
        text = (dest / rel).read_text()
        assert new in text
        compile(text, rel, "exec")
    # Re-installing replaces the previous copy.
    install(tmp_path, archive=data)


def test_fails_loudly_when_upstream_changed(tmp_path):
    data = archive({"mivolo/model/create_timm_model.py": "from timm import something_else\n",
                    "mivolo/model/mivolo_model.py": MODEL})
    with pytest.raises(RuntimeError, match="no longer applies"):
        install(tmp_path, archive=data)
    assert not (tmp_path / "mivolo").exists()
