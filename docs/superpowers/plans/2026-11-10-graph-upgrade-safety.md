# Remaining graph upgrade release work

Implemented upgrade behavior and the runbook are documented in
[README.md](../../../README.md) and [docs/v1.md](../../v1.md).

- [ ] Set the 1.0 support window and deprecation policy before outside users
  depend on the formats. Publish upgrade notes per release in `CHANGELOG.md`;
  keep `/api/v1` responses additive and reserve removals for `/api/v2`.
- [ ] Create the first release tag and verify cross-version CI against an actual
  previous/current release pair. Frozen schema fixtures cover compatibility
  until release tags exist.
- [ ] Complete the upgrade-runbook trial and rollback drill for a tagged release.
  Record that qualification separately from automated Neo4j tests.
