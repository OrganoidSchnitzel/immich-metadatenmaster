"""Command line entry point: `immich-metadatenmaster serve|check|set-date|estimate|calibrate|geotag`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .dates import parse_date_spec, parse_year_bound
from .service import AgeGuesser, build_guesser


def _guesser(settings: Settings) -> AgeGuesser:
    if not settings.configured:
        raise SystemExit("Not set up yet: open the web UI (immich-metadatenmaster serve) and fill in the "
                         "Settings page, or set IMMICH_URL and IMMICH_API_KEY.")
    return build_guesser(settings)


def _progress(label: str):
    def show(done: int, total: int) -> None:
        print(f"\r{label}: {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)
    return show


def cmd_serve(args: argparse.Namespace, settings: Settings) -> None:
    import uvicorn

    from .web.app import create_app

    if not settings.configured:
        print(f"Not set up yet: open http://<this machine>:{args.port}/settings in your browser.", file=sys.stderr)
    uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)


def cmd_check(args: argparse.Namespace, settings: Settings) -> None:
    g = _guesser(settings)
    print(f"Immich {g.immich.server_version()} at {settings.immich_url}")
    people = [p for p in g.immich.people() if p.name]
    with_bd = sum(1 for p in people if p.birth_date)
    print(f"{len(people)} named people, {with_bd} with a birthday")
    print(f"Loading age model {g.backend} …")
    from PIL import Image

    from .faces import load_image

    if args.image:
        img = load_image(Path(args.image).read_bytes())
    else:
        img = Image.new("RGB", (224, 224), (128, 128, 128))
    age = g.estimator.estimate([img])[0]
    print(f"Model works. Estimated age{' of ' + args.image if args.image else ' (blank test image)'}: {age:.1f}")
    print("Calibration:", "yes" if g.calibration_summary()["calibrated"] else "not yet (run `calibrate`)")


def cmd_check_model(args: argparse.Namespace, settings: Settings) -> None:
    """Load the age model and run it once; needs no Immich connection."""
    from PIL import Image

    from .estimators import backend_name, create_estimator
    from .faces import load_image

    print(f"Loading age model {backend_name(settings)} on {settings.device} …")
    estimator = create_estimator(settings)
    img = load_image(Path(args.image).read_bytes()) if args.image else Image.new("RGB", (224, 224), (150, 120, 100))
    age = estimator.estimate([img])[0]
    if not 0 <= age <= 120:
        raise SystemExit(f"The model returned an implausible age: {age}")
    print(f"Model works. Estimated age{' of ' + args.image if args.image else ' (plain test image)'}: {age:.1f}")


def cmd_install_mivolo(args: argparse.Namespace, settings: Settings) -> None:
    from .estimators.mivolo_install import MIVOLO_REF, install

    print(f"Installed MiVOLO {MIVOLO_REF[:7]} to {install()}")


def cmd_set_date(args: argparse.Namespace, settings: Settings) -> None:
    g = _guesser(settings)
    spec = parse_date_spec(args.date)
    album = g.immich.find_album(args.album)
    ids = [a.id for a in g.immich.album_assets(album["id"])]
    print(f"Writing {spec.label} to {len(ids)} photos in '{album['albumName']}'")
    result = g.apply_known_date(ids, spec, keep_order=not args.no_keep_order,
                                remove_from_album=album["id"] if args.remove_from_album else None,
                                progress=_progress("Writing"))
    print(f"Done: {result['written']} photos dated {result['date']}" + _removed(result))


def cmd_estimate(args: argparse.Namespace, settings: Settings) -> None:
    g = _guesser(settings)
    album = g.immich.find_album(args.album)
    assets = g.immich.album_assets(album["id"])
    dated = g.store.dated([a.id for a in assets])
    if not args.include_dated:
        assets = [a for a in assets if a.id not in dated]
    ids = [a.id for a in assets]
    names = {a.id: a.file_name for a in assets}
    results = g.estimate(ids, joint=args.joint, not_before=parse_year_bound(args.not_before, end=False),
                         not_after=parse_year_bound(args.not_after, end=True), progress=_progress("Faces"))
    for aid, res in results.items():
        est = res["estimate"]
        line = f"≈ {est['label']:>8}  ({est['range_label']}, {est['confidence']})" if est else f"  –  {res['message']}"
        print(f"{names[aid][:40]:40} {line}")
    if args.apply:
        out = g.apply_estimates(ids, remove_from_album=album["id"] if args.remove_from_album else None,
                                progress=_progress("Writing"))
        print(f"Wrote {out['written']} dates, skipped {out['skipped']}" + _removed(out))
    else:
        print("Suggestions saved. Review them in the web UI, or re-run with --apply to write them.")


def _removed(result: dict) -> str:
    if "remove_error" in result:
        return f"; removing from the album failed: {result['remove_error']}"
    return f"; {result['removed']} removed from the album" if "removed" in result else ""


def _parse_offset(text: str) -> int:
    """"+1:02:03", "-0:30" or a number of minutes → seconds."""
    s = text.strip()
    sign = -1 if s.startswith("-") else 1
    parts = s.lstrip("+-").split(":")
    try:
        if len(parts) == 1:
            return round(sign * float(parts[0]) * 60)
        h, m, sec = (int(x) for x in (parts + ["0"])[:3])
    except ValueError:
        raise SystemExit(f"Invalid offset '{text}': use e.g. +1:00:00, -0:30 or 90 (minutes).") from None
    return sign * (h * 3600 + m * 60 + sec)


def cmd_geotag(args: argparse.Namespace, settings: Settings) -> None:
    from .geotag import GeoOptions, GeoTagger
    from .gpx import GpxError

    camera_tz = args.camera_tz or settings.timezone
    try:
        ZoneInfo(camera_tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"Unknown time zone '{camera_tz}' (use a name like Europe/Berlin).") from None
    g = _guesser(settings)
    geo = GeoTagger(settings, g.immich, g.store)
    track_ids = []
    for path in args.gpx:
        try:
            t = geo.add_track(Path(path).name, Path(path).read_bytes())
        except GpxError as exc:
            raise SystemExit(f"{path}: {exc}") from exc
        if t["needs_times"]:
            print(f"{path}: has no times (a planned route); set its times on the Locations page.", file=sys.stderr)
        track_ids.append(t["id"])
        print(f"Track '{t['name']}': {t['start'] or '?'} – {t['end'] or '?'}, {t['distance_km']} km")
    album = g.immich.find_album(args.album)["id"] if args.album else None
    opts = GeoOptions(track_ids=track_ids or None, album_id=album, camera_tz=camera_tz,
                      use_photo_tz=not args.ignore_photo_tz, offset_s=_parse_offset(args.offset),
                      tolerance_min=args.tolerance, max_gap_min=args.max_gap, use_nearby=not args.no_nearby)
    try:
        run = geo.find(opts, progress=_progress("Searching"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for s in geo.suggestions().values():
        line = (f"{s['lat']:.5f}, {s['lon']:.5f}  {s['source']:<8} {s['confidence']}" if s["status"] == "ok"
                else f"–  {s.get('reason', '')}")
        print(f"{s['file_name'][:40]:40} {(s['local'] or '')[:19]:19}  {line}")
    print(f"{run['suggested']} of {run['candidates']} photos without a location have a suggestion.")
    if run.get("hint"):
        h = run["hint"]
        print(f"Hint: {h['matches']} photos would fit the tracks with the clock shifted by {h['hours']:+d} h "
              f"(--offset {h['hours']:+d}:00:00); check --camera-tz.")
    if args.apply:
        out = geo.apply(list(geo.suggestions()), fix_time=args.fix_time, progress=_progress("Writing"))
        fixed = f", corrected {out['times_fixed']} times" if out["times_fixed"] else ""
        print(f"Wrote {out['written']} locations{fixed}")
    else:
        print("Suggestions saved. Review them on the Locations page, or re-run with --apply to write them.")


def cmd_calibrate(args: argparse.Namespace, settings: Settings) -> None:
    g = _guesser(settings)
    report = g.calibrate(per_person=args.per_person, progress=_progress("Faces"))
    print(f"Calibrated on {report.samples} faces of {report.people} people.")
    print(f"Mean age error: {report.mae_before} years raw, {report.mae_after} after correction (in-sample).")
    print(f"{'age':>5} {'faces':>7} {'bias':>7} {'spread':>7}")
    for b in report.bins:
        if b["samples"] >= 1:
            print(f"{b['age']:>5.0f} {b['samples']:>7.1f} {b['bias']:>+7.1f} {b['sigma']:>7.1f}")
    print(f"Saved to {g.calibration_path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="immich-metadatenmaster", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("check", help="test the Immich connection and the age model")
    p.add_argument("--image", help="a face photo to run the model on")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("check-model", help="load the age model and run it once (no Immich needed)")
    p.add_argument("--image", help="a face photo to run the model on")
    p.set_defaults(fn=cmd_check_model)

    p = sub.add_parser("install-mivolo", help="download MiVOLO's model code (needed once for the MiVOLO model)")
    p.set_defaults(fn=cmd_install_mivolo)

    p = sub.add_parser("set-date", help="write a known date to every photo of an album")
    p.add_argument("--album", required=True, help="album name or id")
    p.add_argument("--date", required=True, help="e.g. 1987, 06.1987, 14.06.1987, 1985-1989, 1980s")
    p.add_argument("--no-keep-order", action="store_true", help="give all photos the exact same time")
    p.add_argument("--remove-from-album", action="store_true", help="take the photos out of the album afterwards")
    p.set_defaults(fn=cmd_set_date)

    p = sub.add_parser("estimate", help="estimate dates of the photos in an album")
    p.add_argument("--album", required=True, help="album name or id")
    p.add_argument("--joint", action="store_true", help="treat the album as one event")
    p.add_argument("--not-before", help="earliest possible year/date")
    p.add_argument("--not-after", help="latest possible year/date")
    p.add_argument("--include-dated", action="store_true", help="also re-estimate photos dated by this tool")
    p.add_argument("--apply", action="store_true", help="write the estimates to Immich right away")
    p.add_argument("--remove-from-album", action="store_true",
                   help="with --apply: take dated photos out of the album")
    p.set_defaults(fn=cmd_estimate)

    p = sub.add_parser("calibrate", help="learn the model's errors from photos with known dates")
    p.add_argument("--per-person", type=int, default=60, help="max photos per person")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("geotag", help="suggest locations for photos without one from GPX tracks")
    p.add_argument("--gpx", action="append", default=[],
                   help="GPX file to add (repeatable); default: all stored tracks")
    p.add_argument("--camera-tz", help="time zone of the camera clock (default: the TIMEZONE setting)")
    p.add_argument("--ignore-photo-tz", action="store_true", help="ignore time zones stored in the photos")
    p.add_argument("--offset", default="0", help="added to the camera time to get the real time, e.g. +1:00:00")
    p.add_argument("--tolerance", type=float, default=10, help="minutes before/after the track (default 10)")
    p.add_argument("--max-gap", type=float, default=15, help="bridge recording pauses up to this many minutes")
    p.add_argument("--no-nearby", action="store_true", help="do not use nearby photos with a location")
    p.add_argument("--album", help="only photos in this album (name or id)")
    p.add_argument("--apply", action="store_true", help="write the suggestions to Immich right away")
    p.add_argument("--fix-time", action="store_true", help="with --apply and --offset: also correct the photo times")
    p.set_defaults(fn=cmd_geotag)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.load()
    except ValueError as exc:
        raise SystemExit(f"Invalid setting: {exc}") from exc
    args.fn(args, settings)


if __name__ == "__main__":
    main()
