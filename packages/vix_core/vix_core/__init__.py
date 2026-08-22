"""vix_core: shared domain library for the VIX75 microservices platform."""

from vix_core.artifacts import (
    ArtifactIntegrityError,
    artifact_digest,
    load_artifact,
    save_artifact,
    verify_artifact,
)
from vix_core.config import Environment, Settings, get_settings
from vix_core.correlation import (
    CORRELATION_FIELD,
    CORRELATION_HEADER,
    bind_correlation_id,
    get_or_create_correlation_id,
    new_correlation_id,
    stream_fields,
    unbind_correlation_id,
)
from vix_core.logging import RedactingFilter, configure_logging, get_logger
from vix_core.risk import (
    LotSizingResult,
    SizingStatus,
    SymbolConstraints,
    compute_lots,
    validate_stop_distances,
)
from vix_core.schemas import (
    Bar,
    BarSeries,
    ConfluenceComponents,
    Direction,
    OrderRequest,
    OrderResult,
    RegimeSnapshot,
    RegimeState,
    Signal,
    SignalStatus,
    Zone,
    ZoneKind,
    ZoneState,
)

__all__ = [
    "CORRELATION_FIELD",
    "CORRELATION_HEADER",
    "ArtifactIntegrityError",
    "Bar",
    "BarSeries",
    "ConfluenceComponents",
    "Direction",
    "Environment",
    "LotSizingResult",
    "OrderRequest",
    "OrderResult",
    "RedactingFilter",
    "RegimeSnapshot",
    "RegimeState",
    "Settings",
    "Signal",
    "SignalStatus",
    "SizingStatus",
    "SymbolConstraints",
    "Zone",
    "ZoneKind",
    "ZoneState",
    "artifact_digest",
    "bind_correlation_id",
    "compute_lots",
    "configure_logging",
    "get_logger",
    "get_or_create_correlation_id",
    "get_settings",
    "load_artifact",
    "new_correlation_id",
    "save_artifact",
    "stream_fields",
    "unbind_correlation_id",
    "validate_stop_distances",
    "verify_artifact",
]

__version__ = "0.1.0"
