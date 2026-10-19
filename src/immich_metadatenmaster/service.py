"""The date workflows: write known dates, estimate unknown ones from faces, read dates from file names,
apply suggestions, calibrate."""

from __future__ import annotations

import logging
import re
import shutil
import threading
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from PIL import Image

from .calibration import Calibration, CalibrationReport, Sample, default_sigma, fit_calibration
from .config import Settings
from .dates import DateSpec, timestamps, years_between
from .estimators import AgeEstimator, backend_name, create_estimator, default_sigma_scale
from .faces import crop_face, face_size, load_image
from .filename_dates import date_from_name, differs
from .history import History
from .immich import Asset, Face, ImmichClient
from .inference import AgeObservation, DateEstimate, EstimationError, estimate_date
from .store import DatedRecord, Store

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
DESCRIPTION_PREFIX = "Date estimated by Immich Metadatenmaster:"
# Lines written by this tool's predecessor are replaced as well.
_OLD_PREFIXES = (DESCRIPTION_PREFIX, "Date estimated by Immich Age Guesser:")
DB_FILE = "metadatenmaster.sqlite"
_HARD_MIN_FACE_PX = 20
_SCANNER = re.compile(r"scan|coolscan|opticfilm|perfection|reflecta|plustek|pacific image|wolverine|"
                      r"kodak picture saver|ls-\d|fastfoto", re.I)
_BULK = 500


def _noop(done: int, total: int) -> None:
    pass


class AgeGuesser:
    def __init__(self, settings: Settings, immich: ImmichClient, store: Store,
                 estimator_factory: Callable[[Settings], AgeEstimator] = create_estimator) -> None:
        self.settings = settings
        self.immich = immich
        self.store = store
        self.backend = backend_name(settings)
        self._estimator_factory = estimator_factory
        self._estimator: AgeEstimator | None = None
        self._estimator_lock = threading.Lock()
        self._calibration: Calibration | None = None
        self.history = History(store, immich)

    # --- model and calibration --------------------------------------------------------------

    @property
    def estimator(self) -> AgeEstimator:
        with self._estimator_lock:
            if self._estimator is None:
                log.info("Loading age estimator %s", self.backend)
                self._estimator = self._estimator_factory(self.settings)
            return self._estimator

    def adopt_model(self, other: "AgeGuesser") -> None:
        """Reuse an already loaded model of another instance with the same backend (after a settings change)."""
        if (other.backend, other.settings.device) == (self.backend, self.settings.device) and other._estimator:
            self._estimator = other._estimator

    @property
    def calibration_path(self) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", self.backend).strip("-")
        return self.settings.data_dir / f"calibration-{slug}.json"

    @property
    def calibration(self) -> Calibration:
        if self._calibration is None:
            if self.calibration_path.exists():
                self._calibration = Calibration.load(self.calibration_path)
            else:
                self._calibration = Calibration.default(default_sigma_scale(self.settings), self.backend)
        return self._calibration

    # --- faces ------------------------------------------------------------------------------

    def observations(self, asset_id: str) -> tuple[list[AgeObservation], list[str]]:
        """Estimated ages of all named faces with a birthday in the asset, plus notes on skipped faces."""
        notes: list[str] = []
        usable: list[Face] = []
        unnamed = 0
        for face in self.immich.faces(asset_id):
            person = face.person
            if person is None or not person.name:
                unnamed += 1
            elif person.birth_date is None:
                notes.append(f"{person.name} has no birthday")
            else:
                usable.append(face)
        if unnamed:
            notes.append(f"{unnamed} face{'s' if unnamed > 1 else ''} without a name")

        ages = self.store.face_ages([f.id for f in usable], self.backend)
        missing = [f for f in usable if f.id not in ages]
        if missing:
            crops = self._crops(asset_id, missing, notes)
            if crops:
                new = dict(zip(crops, self.estimator.estimate(list(crops.values()))))
                self.store.save_face_ages(new, self.backend)
                ages.update(new)

        obs = [
            AgeObservation(f.person.id, f.person.name, f.person.birth_date, round(float(ages[f.id]), 1), asset_id, f.id)
            for f in usable if f.id in ages and f.person and f.person.birth_date
        ]
        return obs, notes

    def _crops(self, asset_id: str, faces: list[Face], notes: list[str]) -> dict[str, Image.Image]:
        margin = self.estimator.face_margin
        preview = load_image(self.immich.thumbnail(asset_id, "preview")[0])
        original: Image.Image | None = None
        crops: dict[str, Image.Image] = {}
        for face in faces:
            image = preview
            if face_size(face, preview) < self.settings.min_face_px and self.settings.use_original_for_small_faces:
                if original is None:
                    try:
                        original = load_image(self.immich.original(asset_id))
                    except Exception as exc:  # e.g. RAW/HEIC originals Pillow cannot read
                        log.info("Cannot use original of %s: %s", asset_id, exc)
                        original = preview
                image = original
            if face_size(face, image) < _HARD_MIN_FACE_PX:
                notes.append(f"face of {face.person.name if face.person else '?'} is too small")
                continue
            crops[face.id] = crop_face(image, face, margin)
        return crops

    # --- known dates ------------------------------------------------------------------------

    def apply_known_date(self, asset_ids: list[str], spec: DateSpec, *, keep_order: bool = True,
                         remove_from_album: str | None = None, progress: Progress = _noop) -> dict[str, Any]:
        """Write a date the user knows to the assets (in the given order).

        With `remove_from_album`, the dated assets are taken out of that album afterwards.
        """
        day = spec.anchor(self.settings.date_anchor)
        tz = self.settings.timezone
        total = len(asset_ids)
        old = self.history.snapshots(asset_ids)
        written: list[tuple[str, str]] = []
        try:
            if keep_order:
                for i, (aid, stamp) in enumerate(zip(asset_ids, timestamps(day, total, tz))):
                    self.immich.set_date([aid], stamp, tz)
                    written.append((aid, stamp))
                    progress(i + 1, total)
            else:
                stamp = timestamps(day, 1, tz)[0]
                for i in range(0, total, _BULK):
                    self.immich.set_date(asset_ids[i:i + _BULK], stamp, tz)
                    written += [(aid, stamp) for aid in asset_ids[i:i + _BULK]]
                    progress(min(i + _BULK, total), total)
        finally:
            self._log_dates("manual", f"Known date {spec.label}", "Date/Manual", old, written, tz)
        for aid in asset_ids:
            self.store.save_dated(DatedRecord(aid, "manual", spec.label, spec.precision, spec.start, spec.end, day))
        self.store.delete_estimates(asset_ids)
        self.store.delete_suggestions("name_date", asset_ids)
        self._tag("Date/Manual", asset_ids)
        return {"written": total, "date": day.isoformat(), "label": spec.label,
                **self._remove_from_album(remove_from_album, asset_ids)}

    def _log_dates(self, source: str, label: str, tag: str, old: dict[str, tuple[str, dict[str, Any]]],
                   written: list[tuple[str, str]], tz: str, descriptions: dict[str, str] | None = None) -> None:
        changes = []
        for aid, stamp in written:
            new: dict[str, Any] = {"date": stamp, "time_zone": tz}
            if descriptions and aid in descriptions:
                new["description"] = descriptions[aid]
            name, before = old.get(aid, ("", {}))
            changes.append((aid, name, before, new))
        self.history.log("date", source, label, self._tag_value(tag), changes)

    # --- estimation -------------------------------------------------------------------------

    def estimate(self, asset_ids: list[str], *, joint: bool = False, not_before: date | None = None,
                 not_after: date | None = None, progress: Progress = _noop) -> dict[str, dict[str, Any]]:
        """Estimate dates; with joint=True all assets are treated as one event and share one estimate."""
        per_asset: dict[str, tuple[list[AgeObservation], list[str]]] = {}
        errors: dict[str, str] = {}
        for i, aid in enumerate(asset_ids):
            try:
                per_asset[aid] = self.observations(aid)
            except Exception as exc:
                log.exception("Face analysis failed for %s", aid)
                errors[aid] = f"Face analysis failed: {exc}"
            progress(i + 1, len(asset_ids))

        base = {"joint": joint, "backend": self.backend,
                "not_before": not_before.isoformat() if not_before else None,
                "not_after": not_after.isoformat() if not_after else None}
        results: dict[str, dict[str, Any]] = {}

        def result(aid: str, est: DateEstimate | None, message: str, status: str) -> dict[str, Any]:
            notes = per_asset.get(aid, ([], []))[1]
            return {**base, "status": status, "message": message, "notes": notes,
                    "estimate": est.to_dict() if est else None}

        if joint:
            obs = [o for aid in asset_ids for o in per_asset.get(aid, ([], []))[0]]
            with_faces = sum(1 for aid in asset_ids if per_asset.get(aid, ([], []))[0])
            try:
                est: DateEstimate | None = estimate_date(obs, self.calibration, not_before=not_before,
                                                         not_after=not_after, asset_count=with_faces)
                status, message = "ok", f"Joint estimate from {len(obs)} faces in {with_faces} photos"
            except EstimationError as exc:
                est, status, message = None, "no_estimate", str(exc)
            for aid in asset_ids:
                results[aid] = result(aid, est, errors.get(aid, message), "error" if aid in errors else status)
        else:
            for aid in asset_ids:
                if aid in errors:
                    results[aid] = result(aid, None, errors[aid], "error")
                    continue
                try:
                    est = estimate_date(per_asset[aid][0], self.calibration, not_before=not_before,
                                        not_after=not_after)
                    results[aid] = result(aid, est, "", "ok")
                except EstimationError as exc:
                    results[aid] = result(aid, None, str(exc), "no_estimate")

        for aid, res in results.items():
            self.store.save_estimate(aid, res)
        return results

    def apply_estimates(self, asset_ids: list[str], *, remove_from_album: str | None = None,
                        progress: Progress = _noop) -> dict[str, Any]:
        """Write stored estimates (median date) to Immich. Assets without an estimate are skipped."""
        stored = self.store.estimates(asset_ids)
        todo = [aid for aid in asset_ids if (stored.get(aid) or {}).get("status") == "ok"]
        tz = self.settings.timezone
        seen: Counter[date] = Counter()
        old = self.history.snapshots(todo)
        written: list[tuple[str, str]] = []
        descriptions: dict[str, str] = {}
        try:
            for i, aid in enumerate(todo):
                est = DateEstimate.from_dict(stored[aid]["estimate"])
                offset = seen[est.median]
                seen[est.median] += 1
                stamp = timestamps(est.median, offset + 1, tz)[-1]
                self.immich.set_date([aid], stamp, tz)
                written.append((aid, stamp))
                if self.settings.write_description:
                    descriptions[aid] = self._write_description(aid, est)
                label = f"≈ {est.label} ({est.range_label})"
                self.store.save_dated(DatedRecord(aid, "estimate", label, "estimate", est.low, est.high, est.median))
                progress(i + 1, len(todo))
        finally:
            self._log_dates("estimate", "Estimated from faces", "Date/Estimated", old, written, tz, descriptions)
        self.store.delete_estimates(todo)
        self._tag("Date/Estimated", todo)
        return {"written": len(todo), "skipped": len(asset_ids) - len(todo),
                **self._remove_from_album(remove_from_album, todo)}

    def _write_description(self, asset_id: str, est: DateEstimate) -> str:
        people = ", ".join(f"{o.person_name} ≈ {o.estimated_age:.0f} y" for o in est.observations
                           if o.asset_id == asset_id) or "photos of the same event"
        line = f"{DESCRIPTION_PREFIX} {est.label} (90% range {est.range_label}; {people})"
        current = self.immich.asset(asset_id).description
        kept = [ln for ln in current.splitlines() if not ln.startswith(_OLD_PREFIXES)]
        description = "\n".join([*kept, line]).strip()
        self.immich.set_description(asset_id, description)
        return description

    # --- dates from file names --------------------------------------------------------------

    def suggest_name_dates(self, assets: list[Asset], progress: Progress = _noop) -> dict[str, Any]:
        """Store a suggestion for each asset whose file name contains a date that Immich does not show."""
        found: dict[str, dict[str, Any]] = {}
        with_date = 0
        for i, a in enumerate(assets):
            nd = date_from_name(a.file_name)
            if nd is not None:
                with_date += 1
                if differs(nd, a.local_datetime):
                    found[a.id] = {"label": nd.label, "local": nd.to_datetime().isoformat(),
                                   "has_time": nd.time is not None,
                                   "current": a.local_datetime.isoformat() if a.local_datetime else None}
            progress(i + 1, len(assets))
        self.store.delete_suggestions("name_date", [a.id for a in assets])
        self.store.save_suggestions("name_date", found)
        return {"checked": len(assets), "with_date": with_date, "suggested": len(found)}

    def apply_name_dates(self, asset_ids: list[str], *, remove_from_album: str | None = None,
                         progress: Progress = _noop) -> dict[str, Any]:
        """Write the dates found in the file names. Names with only a day keep their order on that day."""
        stored = self.store.suggestions("name_date", asset_ids)
        todo = [aid for aid in asset_ids if aid in stored]
        tz = self.settings.timezone
        per_day: Counter[date] = Counter()
        old = self.history.snapshots(todo)
        written: list[tuple[str, str]] = []
        try:
            for i, aid in enumerate(todo):
                s = stored[aid]
                local = datetime.fromisoformat(s["local"])
                if s["has_time"]:
                    stamp = local.replace(tzinfo=ZoneInfo(tz)).isoformat(timespec="milliseconds")
                else:
                    per_day[local.date()] += 1
                    stamp = timestamps(local.date(), per_day[local.date()], tz)[-1]
                self.immich.set_date([aid], stamp, tz)
                written.append((aid, stamp))
                self.store.save_dated(DatedRecord(aid, "filename", s["label"], "day", local.date(), local.date(),
                                                  local.date()))
                progress(i + 1, len(todo))
        finally:
            self._log_dates("filename", "Dates from file names", "Date/File name", old, written, tz)
        self.store.delete_suggestions("name_date", todo)
        self.store.delete_estimates(todo)
        self._tag("Date/File name", todo)
        return {"written": len(todo), "skipped": len(asset_ids) - len(todo),
                **self._remove_from_album(remove_from_album, todo)}

    def _remove_from_album(self, album_id: str | None, asset_ids: list[str]) -> dict[str, Any]:
        """Take dated assets out of the working album. The photos themselves stay in Immich."""
        if not album_id or not asset_ids:
            return {}
        removed = 0
        try:
            for i in range(0, len(asset_ids), _BULK):
                removed += self.immich.remove_from_album(album_id, asset_ids[i:i + _BULK])
        except Exception as exc:  # the dates are written; only the clean-up failed
            log.warning("Removing from album %s failed: %s", album_id, exc)
            return {"removed": removed, "remove_error": str(exc)}
        return {"removed": removed}

    def _tag_value(self, name: str) -> str | None:
        return f"{self.settings.tag_root}/{name}" if self.settings.tag_root else None

    def _tag(self, name: str, asset_ids: list[str]) -> None:
        tag_assets(self.immich, self._tag_value(name), asset_ids)

    # --- calibration ------------------------------------------------------------------------

    def trusted_date(self, asset: Asset, dated: dict[str, DatedRecord]) -> date | None:
        """Date of an asset if it can serve as ground truth, else None."""
        rec = dated.get(asset.id)
        if rec is not None:
            if rec.source == "manual" and rec.precision in {"day", "month", "year"}:
                return rec.start + (rec.end - rec.start) / 2
            return None
        if asset.exif_datetime is None or not asset.camera_make:
            return None
        if _SCANNER.search(f"{asset.camera_make} {asset.camera_model or ''}"):
            return None
        return asset.exif_datetime.date()

    def calibrate(self, *, per_person: int = 60, progress: Progress = _noop) -> CalibrationReport:
        """Learn the estimator's error model from photos with a trustworthy date."""
        dated = self.store.dated()
        candidates: dict[str, date] = {}
        for person in self.immich.people():
            if not person.name or person.birth_date is None:
                continue
            found = [(d, a.id) for a in self.immich.person_assets(person.id)
                     if (d := self.trusted_date(a, dated)) is not None and a.id not in candidates]
            found.sort()
            step = max(1, len(found) / per_person)
            for k in range(min(per_person, len(found))):
                d, aid = found[int(k * step)]
                candidates[aid] = d

        samples: list[Sample] = []
        outliers = 0
        total = len(candidates)
        for i, (aid, day) in enumerate(candidates.items()):
            try:
                obs, _ = self.observations(aid)
            except Exception as exc:
                log.warning("Skipping %s: %s", aid, exc)
                obs = []
            for o in obs:
                true_age = years_between(o.birth_date, day)
                if not 0 <= true_age <= 100:
                    continue
                # Far-off pairs are almost always a wrong date (e.g. scan date) or a wrong face tag.
                if abs(o.estimated_age - true_age) > 4 * float(default_sigma(true_age, 1.5)):
                    outliers += 1
                    continue
                samples.append(Sample(o.person_id, true_age, o.estimated_age))
            progress(i + 1, total)

        cal, report = fit_calibration(samples, sigma_scale=default_sigma_scale(self.settings), backend=self.backend)
        cal.save(self.calibration_path)
        self._calibration = cal
        log.info("Calibrated on %d faces (%d outliers ignored)", len(samples), outliers)
        return report

    def calibration_summary(self) -> dict[str, Any]:
        cal = self.calibration
        return {"backend": self.backend, "calibrated": not cal.is_default, "samples": cal.samples,
                "created_at": cal.created_at, "people_with_bias": len(cal.person_bias)}


def report_dict(report: CalibrationReport) -> dict[str, Any]:
    return asdict(report)


def tag_assets(immich: ImmichClient, tag: str | None, asset_ids: list[str]) -> None:
    """Tag changed assets. Tagging is a convenience: failures are logged, the change itself stands."""
    if not tag or not asset_ids:
        return
    try:
        for i in range(0, len(asset_ids), _BULK):
            immich.tag_assets(tag, asset_ids[i:i + _BULK])
    except Exception as exc:
        log.warning("Tagging failed: %s", exc)


def open_store(data_dir: Path) -> Store:
    """The local store. A data folder of Immich Age Guesser is taken over (records and cached face ages)."""
    path = data_dir / DB_FILE
    legacy = data_dir / "age-guesser.sqlite"
    if not path.exists() and legacy.exists():
        log.info("Taking over %s", legacy)
        shutil.copyfile(legacy, path)
    return Store(path)


def build_guesser(settings: Settings) -> AgeGuesser:
    """An AgeGuesser talking to the configured Immich server, with its local store in DATA_DIR."""
    immich = ImmichClient(settings.immich_url, settings.immich_api_key)
    return AgeGuesser(settings, immich, open_store(settings.data_dir))
