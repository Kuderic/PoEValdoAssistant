import json
from pathlib import Path

from sniper.models import fnv1a32

VECTORS = Path(__file__).parent / "fixtures" / "frames" / "hash_vectors.json"


def test_fnv1a32_matches_shared_vectors():
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert data["vectors"], "vector file must not be empty"
    for vec in data["vectors"]:
        assert fnv1a32(vec["input"]) == vec["fnv1a32"], vec["input"]
