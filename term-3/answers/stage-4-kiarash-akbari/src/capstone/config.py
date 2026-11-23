"""Read the stage-4 contract, and refuse every key the student has not decided.

``config/capstone.example.toml`` is the tracked copy: it carries the ceilings the
brief states (45 minutes, 500 ms, a 10 % quota) and leaves the student's decisions
empty on purpose. Empty is meaningful, so this module treats it as an error rather
than falling back to a default -- a silent default is how an undecided policy turns
into an undocumented assumption.

Two load modes exist because two different callers need two different things:

* :func:`load_contract` validates the *shape* only. The doctor and the chain plan
  use it to report what is still open, so it must be able to read the example file.
* :func:`load_config` additionally demands that every student decision is written
  down. Everything that touches data goes through this one.

Checklist ids in error messages are ASCII (``4.1``); the checklist file numbers its
items with Persian digits instead, and ``README.md`` states the mapping once.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import ConfigError

#: the value that means "the student has not decided yet"
UNDECIDED = "unset"
#: the explicit answer to "which external source?": none, and a finding in the report
EXTERNAL_NONE = "none"
#: 04 defines a negative review as a score of 1 or 2; changing this changes the task
NEGATIVE_MAX_SCORE = 2
#: a policy may be anything the student can defend, but not this
_POLICY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECTIONS = (
    "time_guard",
    "features",
    "model",
    "capacity",
    "split",
    "budget",
    "external",
)


@dataclass(frozen=True)
class TimeGuard:
    """The single moment every feature must be able to see."""

    decision_column: str
    audit_file: Path


@dataclass(frozen=True)
class Features:
    """Where the feature SQL lives and what its query may cost."""

    schema: str
    sql_migrations_dir: Path
    curated_dir: Path
    feature_query_ms: int


@dataclass(frozen=True)
class ModelDecisions:
    """D1/D2/D3: three labels the brief hands to the student."""

    negative_max_score: int
    no_review: str
    no_approved_at: str
    multi_review: str
    #: the model roster as ``family.variant`` names; empty means "not chosen yet", and the model
    #: step refuses rather than picking two families for the student
    families: tuple[str, ...] = ()
    #: how many trailing months of the training window are held out for model selection
    validation_months: int | None = None
    #: one seed for every estimator that has randomness, so a re-run reproduces the table
    random_seed: int | None = None

    @property
    def undecided(self) -> tuple[str, ...]:
        return tuple(
            f"model.{name}"
            for name in ("no_review", "no_approved_at", "multi_review")
            if getattr(self, name) == UNDECIDED
        )


@dataclass(frozen=True)
class Capacity:
    """The quota is the decision: rank, then take a share."""

    share: float
    report_shares: tuple[float, ...]


@dataclass(frozen=True)
class Split:
    """Time split only, with the boundary as a real date."""

    boundary: date | None
    last_month_as_secondary: bool


@dataclass(frozen=True)
class Budget:
    """45 minutes for the whole chain, measured per layer."""

    chain_minutes: int


@dataclass(frozen=True)
class External:
    """The live signal. Fully declared or fully absent -- never half."""

    name: str
    url: str
    token_env: str
    join_on: str

    @property
    def enabled(self) -> bool:
        """Whether an external signal is in play.

        ``EXTERNAL_NONE`` is a decision, not an empty box: checklist 12.1 allows the project
        to report "no external source, and here is what that costs" instead of fetching one.
        A config that could not say that would force a student to name a source they have not
        verified just to unblock the chain -- which is how an unverified URL becomes a feature.
        """
        return bool(self.name) and self.name != EXTERNAL_NONE


@dataclass(frozen=True)
class CapstoneConfig:
    """Everything the chain reads from disk, and nothing it invents."""

    root: Path
    source: Path
    time_guard: TimeGuard
    features: Features
    model: ModelDecisions
    capacity: Capacity
    split: Split
    budget: Budget
    external: External

    @property
    def audit_path(self) -> Path:
        """The audit file, resolved the way every other relative path is."""
        return (self.source.parent / self.time_guard.audit_file).resolve()

    def undecided_keys(self) -> tuple[str, ...]:
        """Every key that still blocks a phase, in the order a reader would fix them."""
        keys: list[str] = list(self.model.undecided)
        if self.split.boundary is None:
            keys.append("split.boundary")
        if not self.external.enabled and self.external.name != EXTERNAL_NONE:
            # an empty name is an open question; ``none`` is an answer (see External.enabled)
            keys.append("external.name")
        return tuple(keys)


def _section(raw: dict, name: str, source: Path) -> dict:
    try:
        values = raw[name]
    except KeyError:
        raise ConfigError(
            f"{source.name} has no [{name}] section; copy it from config/capstone.example.toml",
            context={"section": name, "config": str(source)},
        ) from None
    if not isinstance(values, dict):
        raise ConfigError(f"[{name}] must be a table, got {type(values).__name__}")
    return values


def _string(values: dict, section: str, key: str, *, allow_empty: bool = False) -> str:
    raw = values.get(key)
    if not isinstance(raw, str):
        raise ConfigError(f"{section}.{key} must be a string, got {type(raw).__name__}")
    stripped = raw.strip()
    if not stripped and not allow_empty:
        raise ConfigError(f"{section}.{key} is empty; it is a student decision, not a default")
    return stripped


def _integer(values: dict, section: str, key: str, *, minimum: int = 1) -> int:
    raw = values.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise ConfigError(f"{section}.{key} must be an integer >= {minimum}, got {raw!r}")
    return int(raw)


def _flag(values: dict, section: str, key: str) -> bool:
    raw = values.get(key)
    if not isinstance(raw, bool):
        raise ConfigError(f"{section}.{key} must be a boolean, got {raw!r}")
    return raw


def _relative_path(root: Path, section: str, key: str, value: str) -> Path:
    """Resolve against the config's own directory, like stages 1 and 3 do."""
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive:
        raise ConfigError(
            f"{section}.{key} must be relative to the config file (00 forbids machine paths), "
            f"got {value!r}"
        )
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.parent):
        raise ConfigError(f"{section}.{key} escapes stage-4/: {value!r}")
    return resolved


def _share(values: dict, section: str, key: str) -> float:
    raw = values.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{section}.{key} must be a number between 0 and 1, got {raw!r}")
    number = float(raw)
    if not 0.0 < number < 1.0:
        raise ConfigError(f"{section}.{key} must be strictly between 0 and 1, got {number}")
    return number


def _policy(values: dict, section: str, key: str) -> str:
    raw = _string(values, section, key)
    if raw == UNDECIDED:
        return raw
    if not _POLICY_RE.match(raw):
        raise ConfigError(
            f"{section}.{key} must be a lowercase identifier (or {UNDECIDED!r} to keep it open), "
            f"got {raw!r}"
        )
    return raw


def _date(values: dict, section: str, key: str) -> date | None:
    raw = _string(values, section, key, allow_empty=True)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ConfigError(
            f"{section}.{key} must be an ISO date like 2018-06-30, got {raw!r}"
        ) from None


def read_raw(path: Path) -> dict:
    """Parse the TOML file itself; every other check lives below this line."""
    if not path.is_file():
        raise ConfigError(f"no config file at {path}; copy config/capstone.example.toml to it")
    with path.open("rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - tomllib tells all
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    missing = [name for name in SECTIONS if name not in raw]
    if missing:
        raise ConfigError(
            f"{path.name} is missing sections: {', '.join(f'[{name}]' for name in missing)}",
            context={"missing": missing, "config": str(path)},
        )
    return raw


def load_contract(path: Path) -> CapstoneConfig:
    """Validate the shape of a config and keep the undecided keys visible."""
    source = path.resolve()
    root = source.parent
    raw = read_raw(source)

    guard_values = _section(raw, "time_guard", source)
    feature_values = _section(raw, "features", source)
    model_values = _section(raw, "model", source)
    capacity_values = _section(raw, "capacity", source)
    split_values = _section(raw, "split", source)
    budget_values = _section(raw, "budget", source)
    external_values = _section(raw, "external", source)

    guard = TimeGuard(
        decision_column=_string(guard_values, "time_guard", "decision_column"),
        audit_file=Path(_string(guard_values, "time_guard", "audit_file")),
    )
    features = Features(
        schema=_string(feature_values, "features", "schema"),
        sql_migrations_dir=_relative_path(
            root,
            "features",
            "sql_migrations_dir",
            _string(feature_values, "features", "sql_migrations_dir"),
        ),
        curated_dir=_relative_path(
            root, "features", "curated_dir", _string(feature_values, "features", "curated_dir")
        ),
        feature_query_ms=_integer(feature_values, "features", "feature_query_ms"),
    )
    model = ModelDecisions(
        negative_max_score=_integer(model_values, "model", "negative_max_score"),
        no_review=_policy(model_values, "model", "no_review"),
        no_approved_at=_policy(model_values, "model", "no_approved_at"),
        multi_review=_policy(model_values, "model", "multi_review"),
        families=_families(model_values),
        validation_months=_optional_integer(model_values, "model", "validation_months"),
        random_seed=_optional_integer(model_values, "model", "random_seed", minimum=0),
    )
    if model.negative_max_score != NEGATIVE_MAX_SCORE:
        raise ConfigError(
            "model.negative_max_score must stay "
            f"{NEGATIVE_MAX_SCORE}: the brief defines a negative review as a score of 1 or 2, "
            "and the primary metric is defined on that label"
        )

    share = _share(capacity_values, "capacity", "share")
    report_shares = _report_shares(capacity_values, share)
    external = _load_external(external_values, root)

    return CapstoneConfig(
        root=root.parent,
        source=source,
        time_guard=guard,
        features=features,
        model=model,
        capacity=Capacity(share=share, report_shares=report_shares),
        split=Split(
            boundary=_date(split_values, "split", "boundary"),
            last_month_as_secondary=_flag(split_values, "split", "last_month_as_secondary"),
        ),
        budget=Budget(chain_minutes=_integer(budget_values, "budget", "chain_minutes")),
        external=external,
    )


_FAMILY_RE = re.compile(r"^[a-z][a-z0-9]*\.[a-z0-9_]+$")


def _families(values: dict) -> tuple[str, ...]:
    """``model.families``: optional, but when present a list of ``family.variant`` names."""
    raw = values.get("families", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError("model.families must be an array of 'family.variant' strings")
    names = tuple(item.strip() for item in raw)
    bad = [name for name in names if not _FAMILY_RE.match(name)]
    if bad:
        raise ConfigError(f"model.families entries must look like 'logreg.c1', got {bad}")
    if len(set(names)) != len(names):
        raise ConfigError(f"model.families names a model twice: {list(names)}")
    return names


def _optional_integer(values: dict, section: str, key: str, *, minimum: int = 1) -> int | None:
    if key not in values:
        return None
    return _integer(values, section, key, minimum=minimum)


def _report_shares(values: dict, share: float) -> tuple[float, ...]:
    raw = values.get("report_shares")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("capacity.report_shares must be a non-empty array of numbers")
    shares: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(f"capacity.report_shares must hold numbers, got {item!r}")
        if not 0.0 < float(item) < 1.0:
            raise ConfigError(f"capacity.report_shares holds {item}, which is not in (0, 1)")
        shares.append(float(item))
    if shares != sorted(shares):
        raise ConfigError("capacity.report_shares must be ascending; the table reads top-down")
    if len(set(shares)) != len(shares):
        raise ConfigError("capacity.report_shares has a duplicate share")
    if share not in shares:
        raise ConfigError(
            f"capacity.share {share} must appear in report_shares {shares}: the operational "
            "point has to be in the reported table"
        )
    return tuple(shares)


def _load_external(values: dict, root: Path) -> External:
    name = _string(values, "external", "name", allow_empty=True).strip()
    url = _string(values, "external", "url", allow_empty=True).strip()
    token_env = _string(values, "external", "token_env", allow_empty=True).strip()
    join_on = _string(values, "external", "join_on", allow_empty=True).strip()
    # token_env stays optional: a free API may need no key at all, and forcing one
    # here would only teach the next person to invent a variable name.
    if name == EXTERNAL_NONE:
        filled = [key for key, value in (("url", url), ("join_on", join_on)) if value]
        if filled or token_env:
            named = ", ".join(filled + (["token_env"] if token_env else []))
            raise ConfigError(
                f"external.name is {EXTERNAL_NONE!r} but {named} is set: declining an external "
                "source and configuring one are different decisions. Leave the rest empty, or "
                "name the source."
            )
        return External(name=EXTERNAL_NONE, url="", token_env="", join_on="")
    required = {"name": name, "url": url, "join_on": join_on}
    declared = [key for key, value in required.items() if value]
    if declared and len(declared) != len(required):
        missing = [key for key, value in required.items() if not value]
        raise ConfigError(
            "external is half declared: it has "
            f"{', '.join(declared)} but is missing {', '.join(missing)}. Fill them all, or "
            "leave them all empty and the chain reports the missing signal as a finding "
            "(checklist 12.1)"
        )
    if not name and token_env:
        raise ConfigError(
            "external.token_env is set while external.name is empty: a credential for no "
            "source is a mistake waiting to be committed"
        )
    if url and not url.startswith(("http://", "https://")):
        raise ConfigError(f"external.url must be an http(s) URL, got {url!r}")
    if token_env and not _ENV_NAME_RE.match(token_env):
        raise ConfigError(
            f"external.token_env must be an environment variable name, got {token_env!r}"
        )
    if join_on and not _POLICY_RE.match(join_on):
        raise ConfigError(
            f"external.join_on must be a lowercase identifier like 'date' or 'city', got "
            f"{join_on!r}"
        )
    if name and not (root.parent / "decisions.md").is_file():
        raise ConfigError(
            "external.name is set but stage-4/decisions.md does not exist: the source and its "
            "hypothesis are the same decision (checklist 12.1, 9.4)"
        )
    return External(name=name, url=url, token_env=token_env, join_on=join_on)


def load_config(path: Path) -> CapstoneConfig:
    """A config the chain may act on: valid shape, and every decision written down."""
    config = load_contract(path)
    undecided = config.undecided_keys()
    if undecided:
        raise ConfigError(
            "these keys are student decisions and are still undecided: "
            + ", ".join(undecided)
            + ". Write them in config/capstone.toml and record the reasoning in decisions.md "
            "(ROADMAP phase 1). An undecided policy must not be replaced by a default.",
            context={"undecided": list(undecided), "config": str(path)},
        )
    return config


def documented_decisions(decisions_path: Path, config: CapstoneConfig) -> list[str]:
    """Which decisions are still missing from ``decisions.md``.

    The brief asks for the reasoning in a file, so this only checks that each policy
    name and the boundary date appear there. It cannot judge the argument; that is
    what the defence session is for.
    """
    if not decisions_path.is_file():
        return [f"{decisions_path.name} is missing (checklist 9.4)"]
    text = decisions_path.read_text(encoding="utf-8")
    wanted: list[str] = [
        value
        for value in (
            config.model.no_review,
            config.model.no_approved_at,
            config.model.multi_review,
            config.external.name,
        )
        if value and value != UNDECIDED
    ]
    if config.split.boundary is not None:
        wanted.append(config.split.boundary.isoformat())
    return [term for term in wanted if term not in text]
