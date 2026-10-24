import httpx
import pytest

from immich_metadatenmaster.geocode import GeocodeError, parse_coordinates, search

PHOTON = {"features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.5754, 48.1374]},
     "properties": {"name": "Marienplatz", "city": "München", "postcode": "80331", "state": "Bayern",
                    "country": "Deutschland", "osm_value": "square",
                    "extent": [11.574, 48.138, 11.577, 48.136]}},
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10.9853, 47.4211]},
     "properties": {"name": "Zugspitze", "state": "Bayern", "country": "Deutschland", "osm_value": "peak"}},
    {"type": "Feature", "geometry": None, "properties": {"name": "broken"}},
]}


def photon(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=PHOTON)
    return httpx.MockTransport(handler)


def test_search_photon():
    calls = []
    results = search("https://photon.test/", "marienplatz", lang="de-DE", near=(48.1, 11.5), transport=photon(calls))
    assert [r["name"] for r in results] == ["Marienplatz", "Zugspitze"]
    m = results[0]
    assert m["detail"] == "80331 München, Bayern, Deutschland" and (m["lat"], m["lon"]) == (48.1374, 11.5754)
    assert m["bounds"] == [[48.136, 11.574], [48.138, 11.577]] and results[1]["bounds"] is None
    q = calls[0].url.params
    assert calls[0].url.path == "/api" and q["q"] == "marienplatz" and q["lang"] == "de" and q["lat"] == "48.1"
    assert "immich-metadatenmaster" in calls[0].headers["user-agent"]


def test_language_not_supported_by_photon_is_left_out():
    calls = []
    search("https://photon.test", "x", lang="ru", transport=photon(calls))
    assert "lang" not in calls[0].url.params


@pytest.mark.parametrize("text, expected", [
    ("47.4211, 10.9853", (47.4211, 10.9853)), ("47.4211 10.9853", (47.4211, 10.9853)),
    ("-33.86;151.21", (-33.86, 151.21)), ("47,4211 10,9853", (47.4211, 10.9853)),
    ("Zugspitze", None), ("95, 10", None),
])
def test_coordinates(text, expected):
    assert parse_coordinates(text) == expected


def test_coordinates_need_no_geocoder():
    calls = []
    results = search("", "47.4211, 10.9853", transport=photon(calls))
    assert results[0]["kind"] == "coordinates" and calls == []


def test_errors():
    with pytest.raises(GeocodeError, match="switched off"):
        search("", "Zugspitze")
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(GeocodeError, match="503"):
        search("https://photon.test", "Zugspitze", transport=down)


def test_place_label():
    from immich_metadatenmaster.geocode import place_label
    hut = {"name": "Höllentalangerhütte", "type": "house", "city": "Grainau", "country": "Deutschland"}
    assert place_label(hut) == "Grainau, Deutschland"  # a house name is not a place
    assert place_label({**hut, "type": "other"}) == "Höllentalangerhütte, Grainau"
    assert place_label({"name": "Bayern", "type": "state", "country": "Deutschland"}) == "Bayern, Deutschland"
    assert place_label({}) == ""


def test_reverse():
    from immich_metadatenmaster.geocode import reverse
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"features": [{"properties": {"name": "Zugspitze", "type": "other",
                                                                      "county": "Garmisch-Partenkirchen"}}]})

    assert reverse("https://photon.test", 47.42111, 10.98531, lang="de", transport=httpx.MockTransport(handler)) \
        == "Zugspitze, Garmisch-Partenkirchen"
    q = calls[0].url.params
    assert calls[0].url.path == "/reverse" and q["lat"] == "47.42111" and q["lang"] == "de"
    empty = httpx.MockTransport(lambda r: httpx.Response(200, json={"features": []}))
    assert reverse("https://photon.test", 0.5, 0.5, transport=empty) == ""
