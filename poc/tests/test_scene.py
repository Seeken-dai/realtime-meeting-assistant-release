import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suggest import normalize_scene, scene_config


assert normalize_scene("sales") == "sales"
assert normalize_scene("unknown") == "general"
assert "商务承诺" in scene_config("sales")["categories"]
assert "验收标准" in scene_config("requirements")["categories"]
assert scene_config("general")["minutes"]

print("ok: scene normalization + prompt/minutes configuration")
