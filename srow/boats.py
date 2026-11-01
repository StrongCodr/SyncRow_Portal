"""Boat classes, built-in rigging defaults and user boat profiles.

A profile starts from a class and stores only the fields the user overrode; everything else
falls through to the class default (see DEFAULTS). Profiles and the per-class default profile
are stored in InfluxDB next to the run names (InfluxService.fetch_boat_profiles).

Defaults: masses are World Rowing minimum boat weights; rigging follows club norms (Concept2
and row2k rigging guides); hull lengths are current Empacher moulds; pin height above the water
is the oarlock height above the seat plus the seat's height above the waterline, which has no
published standard and is the least certain number here.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

CLASSES = ("1x", "2x", "2-", "4x", "4-", "4+", "8+")

# field -> (label, unit, decimals)
FIELDS = {
    "oar_len_m": ("Oar length", "m", 2),
    "inboard_m": ("Inboard", "m", 2),
    "span_m": ("Span / spread", "m", 2),
    "pin_height_m": ("Pin above water", "m", 2),
    "hull_len_m": ("Hull length", "m", 1),
    "hull_mass_kg": ("Hull mass", "kg", 0),
}

DEFAULTS: dict[str, dict[str, float]] = {
    "1x": dict(oar_len_m=2.88, inboard_m=0.88, span_m=1.59, pin_height_m=0.25, hull_len_m=8.2, hull_mass_kg=14),
    "2x": dict(oar_len_m=2.88, inboard_m=0.88, span_m=1.59, pin_height_m=0.24, hull_len_m=9.8, hull_mass_kg=27),
    "4x": dict(oar_len_m=2.88, inboard_m=0.88, span_m=1.59, pin_height_m=0.24, hull_len_m=13.5, hull_mass_kg=52),
    "2-": dict(oar_len_m=3.72, inboard_m=1.16, span_m=0.85, pin_height_m=0.26, hull_len_m=10.0, hull_mass_kg=27),
    "4-": dict(oar_len_m=3.72, inboard_m=1.14, span_m=0.85, pin_height_m=0.26, hull_len_m=13.5, hull_mass_kg=50),
    "4+": dict(oar_len_m=3.72, inboard_m=1.14, span_m=0.85, pin_height_m=0.26, hull_len_m=13.7, hull_mass_kg=51),
    "8+": dict(oar_len_m=3.72, inboard_m=1.14, span_m=0.85, pin_height_m=0.26, hull_len_m=17.5, hull_mass_kg=96),
}

FALLBACK_CLASS = "2x"


def is_sculling(cls: str) -> bool:
    return cls.endswith("x")


@dataclass(frozen=True)
class Rigging:
    cls: str
    oar_len_m: float
    inboard_m: float
    span_m: float
    pin_height_m: float
    hull_len_m: float
    hull_mass_kg: float
    profile_id: str | None = None
    profile_name: str | None = None

    @property
    def outboard_m(self) -> float:
        return self.oar_len_m - self.inboard_m

    def as_dict(self) -> dict:
        d = asdict(self); d["outboard_m"] = self.outboard_m
        return d


@dataclass
class BoatProfile:
    """A user's boat: a name, the class it starts from, and the fields overridden from the class
    default. `deleted` hides it; the record is kept so runs that reference it still resolve."""
    id: str
    name: str
    cls: str
    overrides: dict[str, float]
    deleted: bool = False

    @classmethod
    def from_json(cls, id: str, d: dict) -> "BoatProfile":
        ov = {k: float(v) for k, v in (d.get("overrides") or {}).items() if k in FIELDS and v is not None}
        c = d.get("cls") if d.get("cls") in CLASSES else FALLBACK_CLASS
        return cls(id=id, name=str(d.get("name") or id), cls=c, overrides=ov, deleted=bool(d.get("deleted")))

    def to_json(self) -> dict:
        return {"name": self.name, "cls": self.cls, "overrides": self.overrides, "deleted": self.deleted}


def resolve(cls: str | None, profile: BoatProfile | None = None) -> Rigging:
    """The rigging to draw with: the profile's overrides on top of its class default, else the class
    default, else the fallback class."""
    c = profile.cls if profile is not None else (cls if cls in CLASSES else FALLBACK_CLASS)
    vals = dict(DEFAULTS[c])
    if profile is not None:
        vals.update(profile.overrides)
    return Rigging(cls=c, profile_id=profile.id if profile else None, profile_name=profile.name if profile else None, **vals)


def pick_profile(profiles: dict[str, BoatProfile], defaults: dict[str, str], profile_id: str | None, cls: str | None) -> BoatProfile | None:
    """The run's own profile if it names one, else the user's default profile for the run's class."""
    if profile_id and profile_id in profiles:
        return profiles[profile_id]
    if cls and defaults.get(cls) in profiles and not profiles[defaults[cls]].deleted:
        return profiles[defaults[cls]]
    return None
