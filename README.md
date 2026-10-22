# Immich Metadatenmaster

Fill in missing metadata in [Immich](https://immich.app): **dates** and **locations**.
The tool looks at your photos and *suggests* values. Nothing is written to Immich until you have
reviewed the suggestions and clicked a write button, and every write is logged with the previous
values and can be undone.

| | Where the suggestion comes from |
|---|---|
| 📅 **Date you know** | You type it: `1987`, `06.1987`, `14.06.1987`, `1985-1989` or `1980s`. |
| 📅 **Date from faces** | How old the people in the photo look, combined with their birthdays in Immich. For scanned analog photos. |
| 📅 **Date from the file name** | `IMG-20190713-WA0001.jpg`, `PXL_20230101_123456789.jpg`, `WhatsApp Image 2020-01-01 at 12.00.00.jpeg`, `1987-07-01 Holiday.jpg` … For messenger photos and scans whose date Immich got wrong. |
| 📍 **Location from a GPX track** | Upload tracks from Komoot, Strava, Garmin etc. Photos without a location get the position you were at when they were taken. |
| 📍 **Location from nearby photos** | A phone photo with GPS taken a few minutes before or after (e.g. your camera has no GPS, your phone does). |
| 📍 **Location from the same photo** | Another version of the same shot that has a location: `IMG_1234.CR2` → `IMG_1234-HDR.dng`, `IMG_1234_edited.jpg`. |
| 📍 **Location on the map** | Search a place or address (or paste coordinates) and place the selected photos there, or click on the map. |

Everything runs locally. Photos never leave your server. It combines two earlier tools: Immich Age
Guesser (dates from faces) and a geotagging notebook (GPX → RAW files).

## Locations from GPX tracks

1. Open **Locations** and drop your GPX files onto the page. From Komoot: open a *completed* tour
   → ⋯ → *Download GPX*. Strava, Garmin Connect, OsmAnd and most other apps export GPX too.
   - A **planned** route has no times. The tool asks when you walked it and assumes a constant
     pace; those suggestions are marked *low confidence*.
2. Tick the tracks to use and click **Find suggestions**. The tool searches Immich for photos and
   videos *without* a location in the tracks' time span (optionally only in one album, or in an
   extra time range) and computes a position for each:
   - **On the track**: the position is interpolated between the two track points around the
     photo's time. Photos a little before the start or after the end (*tolerance*, default
     10 min) get the first/last point. A pause in the recording is only bridged if you stayed in
     place (e.g. a break at a hut) or the pause is short (default 15 min); otherwise the photo
     gets the nearest point if it is within the tolerance.
   - **Same-name file** has priority (an HDR merge gets exactly the location of its RAW file),
     then the **GPX track**, then **nearby photos** with a location (interpolated between the
     photos before and after, up to 10 min apart). Photos of the *same camera* come first and are
     compared by that camera's own clock, so a wrong clock does not matter; another camera (e.g. your
     phone) only counts if the same camera has nothing close, with a lower confidence. If the photos
     before and after are too far apart for the time between them (faster than 250 km/h: a wrong
     clock or a flight), the nearer one is used instead of a point in between.
3. Review on the map and in the list: each suggestion shows its source, how far it is in time
   from real track data, and a confidence. Select (all, high confidence only, one camera …),
   discard what looks wrong, or place selected photos yourself (see below). Then click
   **Write locations**.

Each suggestion shows the name of the place (“📍 Höllentalangerhütte, Grainau”), so you can check
it without zooming in. Immich fills in its own place names (city, country) after writing. Immich has
no API to set the altitude, so the GPX elevation is only shown, not written.

### Placing photos by address

The map has a search box: type a place, an address or a landmark (“Marienplatz München”,
“Zugspitze”), or paste coordinates (`47.4211, 10.9853`, e.g. copied from Google Maps). Pick a
result, and click **Place … selected photos here** in its bubble. Or click **Place selected on the
map…** and click the spot.

This works without GPX tracks too: untick the tracks and choose an **album** (all its photos
without a location are listed), or enter a time range. Tick *Also list photos that already have a
location* to correct a wrong location.

The search uses [Photon](https://github.com/komoot/photon), the OpenStreetMap geocoder of Komoot
(`https://photon.komoot.io`). This server sends it the search text and the map centre (to prefer
nearby results), and, for the place names of suggestions, their positions rounded to about 100 m
(each place is asked about once, then remembered). You can point it at your own Photon instance,
switch off the place names, or switch it off completely in the settings; typed coordinates always
work.

### Photos that need no location

Screenshots, scanned documents, memes …: select them (the **Screenshots** button selects file names
like `Screenshot_…` and graphics without a camera) and click **No location needed**. Later searches
skip them. The list is at the bottom of the page, where you can unmark them again. This is only
remembered by this tool; nothing is written to Immich.

### Camera clocks and time zones

GPX times are UTC; most system cameras store only their clock time without a time zone. The tool
therefore needs to know:

- **Camera time zone**: the zone the camera's clock was set to (default: the time zone from the
  settings). Photos that store a time zone (phones, some newer cameras) use their own, unless you
  untick *Trust the time zone stored in a photo*.
- **Clock offset**: if the camera's clock was simply wrong, e.g. `+1:00:00` when it was still on
  winter time, or `-0:03:20` when it ran 3 min 20 s fast. When writing, you can *also correct the
  photo times* by the offset (this part can be undone in History).

After a search, **Camera clocks** lists every camera with photos without a location, each with its
own offset:

- If a camera's photos would fit the tracks much better with its clock shifted by whole hours (a
  wrong time zone or summer time), it says so: **Shift by −2 h and search again**.
- **Sync with another camera…** works out an offset for you: pick a photo of that camera, then the
  photo of your phone (whose clock sets itself) showing the same moment. The difference of their
  times is the offset.

## Dates

The date tools work per Immich album (a batch of scans, a WhatsApp export …):

- **You know the date** (even just the year or month): type it and click **Write date**. By
  default the photos keep their file order in the timeline (1 minute apart).
- **Read file names**: suggests the date (and time, if present) from the file name, for photos
  whose date in Immich is different. Names with only a day keep their order on that day.
- **Estimate from faces**: see below. You review the suggestions and then write them.

### How the estimate from faces works

1. Immich has already found the faces (bounding boxes) and you have named the people.
2. Each named face with a known birthday is cropped from Immich's preview. Small faces in group
   photos are cropped from the full-resolution scan. An age model estimates how old the person looks:
   [MiVOLO v2](https://github.com/WildChlamydia/MiVOLO) by default, or a vision LLM via Ollama.
3. For every possible date *t*, the person's true age would be *t − birthday*. The model's error is
   modelled as a normal distribution. It is about ±1 year for toddlers, ±4 years at 30 and ±8 at 70.
   A small "outlier" component means a mistagged face can't veto the rest. Multiplying the
   likelihoods of all faces gives a probability curve over the date. The suggestion is its median,
   together with a 90% range.
4. **Calibration** (optional, recommended) learns how wrong the model actually is on *your* photos.
   It uses digital photos with EXIF dates, plus scans you have already dated with this tool. It
   learns the bias per age group, the spread, and a correction per person, e.g. "the model always
   thinks Grandma is 5 years younger".

What precision to expect:

| Who is in the photo | Typical 90% range |
|---|---|
| A small child | about ±1 year |
| A school child or teenager | ±2–3 years |
| A single adult | ±6–10 years (still tells the 70s from the 90s) |
| Several family members of different generations | often ±1–2 years |

Photos without any known face can only be dated as part of an event. Select them together with
photos that have faces and tick *same event*.

### Versions with a different date (HDR, panoramas, edits)

HDR merges and panoramas (`IMG_1234-HDR.dng`, `IMG_1234-Pano.dng`), edited copies
(`IMG_1234_edited.jpg`) and second downloads (`IMG_1234 (1).jpg`) often lose the capture date, so
Immich shows them on the day they were made. **Find versions** on the Dates page looks for them in the
whole library (or one album), finds their original (`IMG_1234.CR2`) and suggests its date, and its
location if the version has none. If a camera's counter started again and there are several
`IMG_1234.CR2` from different years, the one in the same folder wins; otherwise the version is
skipped rather than guessed.

### Workflow for scans

1. Scan photos and upload them into an **Immich album**. There are two ways to organise this:
   - **One working album** (e.g. "Scans – to date") that you reuse for every batch. Tick
     *Remove photos from this album once their date is written*: dated photos leave the album
     but stay in Immich, so the album only ever holds what is left to do.
   - **One album per batch** (a box, an album, a film), which you can delete when you are done.
     Deleting an album in Immich never deletes its photos.
2. Wait for Immich's face detection, then name the people in Immich.
3. Open the tool, go to **People & birthdays**, and fill in the missing birthdays. They are saved
   in Immich.
4. Open the album under **Dates**:
   - Select photos whose date you know, type the date and click **Write date**.
   - Select the rest (**Not dated yet**) and click **Estimate**. You can limit the range, for
     example "not after 1995" if the photos came from a box you know is older. Tick *same event*
     if the photos belong together.
   - Check the suggestions. Faces that don't fit the others are flagged ("check face/birthday").
     Accept them one by one, or select several and click **Write suggestions**.
5. Every so often, run **Calibration** on the Dates page. Each scan you date by hand makes the
   next estimates better.

Immich has no field for "approximate date". A year is written as 2 July, the middle of the year;
this can be switched to 1 January in the settings. Estimated photos also get a line in their
description, for example:
`Date estimated by Immich Metadatenmaster: 1987 (90% range 1985–1989; Anna ≈ 7 y, Bernd ≈ 36 y)`.

## Handy to know

- **Large view:** double-click a photo (or press <kbd>V</kbd>, or click ⤢) to see it big. <kbd>←</kbd>
  <kbd>→</kbd> browse, <kbd>Space</kbd> selects the photo you are looking at, <kbd>Esc</kbd> closes.
- **Selecting:** shift-click selects everything between the last clicked photo and this one;
  <kbd>A</kbd> selects all, <kbd>Esc</kbd> clears. On the Locations page, **Select day** selects a whole day.
- **Moving a photo on the map:** drag its dot.
- **Long jobs** (estimating, searching, writing) run on the server: you can close or reload the page,
  the progress shows up again, and the top bar shows what is running. **Cancel** stops a job; what
  was already written stays written and is in the History.
- **Videos** are dated too (e.g. `VID-20190713-WA0001.mp4` from WhatsApp).

## History, tags and undo

- Changed photos are tagged, so you can find them in Immich: `Metadatenmaster/Date/Manual`,
  `…/Date/Estimated`, `…/Date/File name`, `…/Date/From original`, `…/Location/GPX`, `…/Location/Nearby photo`,
  `…/Location/Same-name file`, `…/Location/Map`. The tag root can be changed or switched off in the
  settings.
- **History** lists every write with the values from before. **Undo** puts the previous dates
  (and descriptions) or locations back and removes the tag. Photos that were changed in Immich
  since are left alone.
- Removing a location: Immich's API has no way to empty it, but Immich treats exactly `0, 0` as
  “no location” and clears it. So undoing a location on a photo that had none writes `0, 0` and
  then checks that Immich cleared it; if it did not, the written location is put back (no photo is
  left in the Atlantic) and the History page says so.

## Installation

A ready-made Docker image is published at `ghcr.io/organoidschnitzel/immich-metadatenmaster`.
Everything else is set up in the browser.

### Option A: next to Immich, in Immich's own `docker-compose.yml` (recommended)

Add this service to the `services:` section of the `docker-compose.yml` you run Immich with:

```yaml
  immich-metadatenmaster:
    container_name: immich_metadatenmaster
    image: ghcr.io/organoidschnitzel/immich-metadatenmaster:latest
    ports:
      - "8080:8080"
    volumes:
      - ./metadatenmaster-data:/data
    restart: always
```

Then run `docker compose up -d` in that folder, open `http://<server>:8080` and fill in the
setup page. Use **`http://immich-server:2283`** as the Immich URL: both containers share Immich's
network, so the tool reaches Immich by its service name.

### Option B: on its own

Download [`docker-compose.yml`](docker-compose.yml) into an empty folder, run
`docker compose up -d`, and open `http://<server>:8080`. Use your server's address as the
Immich URL, e.g. `http://192.168.1.10:2283`.

### The setup page

The first visit opens the setup page:

1. In Immich, open *Account Settings → API Keys* and create a key. Either give it all permissions
   or these: `album.read`, `albumAsset.delete`, `asset.read`, `asset.view`, `asset.download`,
   `asset.update`, `face.read`, `person.read`, `person.update`, `tag.create`, `tag.asset`.
2. Paste the URL and the key, click **Test connection**, then **Save**.

You can change everything later under **Settings**. The settings, the tool's records and the
downloaded model (about 100 MB, fetched on the first estimate) live in the `/data` volume.

> The web UI has no login of its own and holds an API key that can change your photos. Only
> expose it on your LAN, or put it behind your reverse proxy's authentication.

**Updating:** `docker compose pull && docker compose up -d`.

### Option C: Unraid

*Docker → Add Container*, then either fill in the form by hand or use the template:

- **By hand:** Repository `ghcr.io/organoidschnitzel/immich-metadatenmaster:latest` (all lowercase),
  port `8080` → container port `8080`, path `/mnt/user/appdata/immich-metadatenmaster` → container
  path `/data`, optionally variable `TZ` = `Europe/Berlin`.
- **With the template:** run this once in the Unraid terminal, then pick
  *immich-metadatenmaster* in the *Template* dropdown of *Add Container*:

  ```bash
  wget -O /boot/config/plugins/dockerMan/templates-user/my-immich-metadatenmaster.xml \
    https://raw.githubusercontent.com/OrganoidSchnitzel/immich-metadatenmaster/HEAD/unraid/immich-metadatenmaster.xml
  ```

Then open the WebUI. As the Immich URL use `http://<unraid-ip>:2283`, the port your Immich
container publishes.

The tool is not in the *Apps* tab (Community Applications), so searching there finds nothing.

### Coming from Immich Age Guesser

This tool replaces Immich Age Guesser. Point the new container's `/data` at the old data folder
(e.g. `./age-guesser-data:/data`): settings, calibration, cached face ages and the record of dated
photos are taken over on the first start. If you had kept the old tag root `Age Guesser`, new
tags go below it; change it in the settings to `Metadatenmaster` if you like.

### Without Docker (Python 3.10+)

```bash
python -m venv .venv && . .venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[mivolo]"
immich-metadatenmaster install-mivolo         # MiVOLO's model code is not on PyPI
immich-metadatenmaster serve --port 8080      # then open http://localhost:8080
```

### CPU or GPU?

Use the **CPU of the home server**. Age estimation is a small model: roughly 0.1–0.3 s per face
on an i5-13500, so a batch of 500 scans takes minutes. It runs next to Immich and is always
available, with no dual-boot involved. A GPU is only worth it for tens of thousands of photos. To
use the RX 6800 anyway, build the image yourself with a ROCm PyTorch
(`docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/rocm6.2 .`), pass the GPU
into the container (`/dev/kfd`, `/dev/dri`) and set the device to `cuda`. PyTorch uses the same
name for ROCm.

## Command line

Everything the UI does is also available on the command line, e.g. with
`docker exec -it immich_metadatenmaster immich-metadatenmaster …`:

```bash
immich-metadatenmaster set-date    --album "Box 3" --date 1987 [--remove-from-album]
immich-metadatenmaster estimate    --album "Box 3" [--joint] [--not-before 1975] [--not-after 1995] [--apply [--remove-from-album]]
immich-metadatenmaster calibrate   [--per-person 60]
immich-metadatenmaster geotag      --gpx tour.gpx [--gpx more.gpx] [--camera-tz Europe/Berlin] [--offset +1:00:00]
                                   [--camera-offset "Canon EOS 80D=-0:03:20"] [--album "Alps 2024"]
                                   [--apply [--fix-time]]
immich-metadatenmaster check       [--image face.jpg]   # tests the Immich connection and the model
immich-metadatenmaster check-model [--image face.jpg]   # tests only the model
```

`estimate` and `geotag` without `--apply` only store suggestions. You can review them in the UI.

## Configuration

Normally everything is set on the **Settings** page and stored in `/data/config.json`.
Alternatively, any setting can be given as an environment variable (see
[`.env.example`](.env.example)). A setting given by an environment variable takes precedence and
is shown read-only on the Settings page.

| Variable | Default | |
|---|---|---|
| `IMMICH_URL`, `IMMICH_API_KEY` | – | Immich server and API key |
| `TIMEZONE` (or `TZ`) | `UTC` | time zone for written dates, e.g. `Europe/Berlin` |
| `DATE_ANCHOR` | `middle` | `middle` or `start` of a year/month |
| `REMOVE_FROM_ALBUM` | `false` | default for "remove photos from this album once dated" |
| `AGE_ESTIMATOR` | `mivolo` | or `ollama` (then `OLLAMA_URL`, `OLLAMA_MODEL`) |
| `TAG_ROOT` | `Metadatenmaster` | empty to disable tagging |
| `MAP_TILE_URL`, `MAP_ATTRIBUTION` | OpenStreetMap | background map of the Locations page |
| `GEOCODER_URL` | `https://photon.komoot.io` | address search on the map; empty to switch it off |
| `PLACE_NAMES` | `true` | show place names next to location suggestions (uses the geocoder) |
| `WRITE_DESCRIPTION` | `true` | add the explanation line to estimated photos |
| `DATA_DIR` | `/data` in Docker | where settings, GPX tracks, records and the model are stored |

## Good to know

- **Better scans give better estimates.** Faces should be at least ~100 px. Scan prints at
  600 dpi or more, especially group photos.
- **Calibration and scans.** Scanners also write EXIF dates, but those record the scan date.
  Photos from known scanner models are ignored, and so are photos whose estimate is wildly off,
  which usually means a wrong date or a wrong face tag. Scans you dated by year, month or day with
  this tool do count as ground truth.
- **Age models have biases.** They can be off for faded black-and-white prints, strong makeup,
  costumes, and some ethnicities or age groups. Calibration corrects part of this, per person too.
- **Local data.** The tool keeps its settings and its own records in `DATA_DIR`: uploaded GPX
  tracks, cached face ages, pending suggestions, and what was written (History). Deleting the
  folder loses no data in Immich.
- **Map tiles.** The Locations page loads its background map from OpenStreetMap in your browser.
  Another tile server can be set in the settings.
- **MiVOLO license.** MiVOLO's code is Apache-2.0. The model weights are downloaded from
  [Hugging Face](https://huggingface.co/iitolstykh/mivolo_v2) under the license stated there;
  check it before using the tool for anything beyond private use.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests run the whole pipeline against a fake Immich server (`tests/conftest.py`) and a fake age
model that reads ages from the test images. That covers cropping, inference, calibration, GPX
parsing and matching, file-name dates, the write-back with history and undo, the setup page and
the web API. In addition, CI builds the Docker image, starts it,
and runs `immich-metadatenmaster check-model`, which downloads the real MiVOLO model and runs it once.
