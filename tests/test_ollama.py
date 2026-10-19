import json

import httpx
import pytest
from PIL import Image

from immich_metadatenmaster.estimators.ollama import OllamaEstimator, parse_age


@pytest.mark.parametrize("text, age", [('{"age": 34}', 34.0), ('{"age": "about 40"}', 40.0), ("I think 7.5", 7.5)])
def test_parse_age(text, age):
    assert parse_age(text) == age


def test_parse_age_fails():
    with pytest.raises(ValueError):
        parse_age("no idea")


def test_request():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"response": '{"age": 12}'})

    est = OllamaEstimator("http://ollama", "gemma3", transport=httpx.MockTransport(handler))
    assert est.estimate([Image.new("RGB", (50, 50))]) == [12.0]
    assert seen["model"] == "gemma3" and seen["images"] and seen["stream"] is False
