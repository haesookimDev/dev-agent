"""Immutable V1 retention-state constraint shared with the deployed migration."""

ARTIFACT_RETENTION_V1 = (
    "(expired_at IS NULL AND purged_at IS NULL AND retention_days IS NULL "
    "AND retention_sha256 IS NULL) OR "
    "(expired_at IS NOT NULL AND retention_days IS NOT NULL "
    "AND retention_days >= 1 AND retention_days <= 36500 "
    "AND retention_sha256 IS NOT NULL AND length(retention_sha256) = 64 "
    "AND (purged_at IS NULL OR purged_at >= expired_at))"
)
