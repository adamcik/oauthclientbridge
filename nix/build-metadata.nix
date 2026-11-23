# Flake snapshots have no Git history. Use the application's input identity, never
# the downstream configuration revision or the operator's runtime environment.
{
  lib,
  source,
  overrides,
  baseVersion,
}: let
  sourceRevision = source.rev or (lib.removeSuffix "-dirty" (source.dirtyRev or ""));
  revision = overrides.revision or null;
  resolvedRevision =
    if revision != null
    then revision
    else if sourceRevision != ""
    then sourceRevision
    else null;
  sourceDirty =
    if source ? rev
    then false
    else if source ? dirtyRev
    then true
    else null;
  dirty =
    if sourceDirty != null
    then sourceDirty
    else overrides.dirty or null;
in
  assert lib.assertMsg ((overrides.version or null) == null) "Build versions are derived from project.version and source identity; remove the version override.";
  assert lib.assertMsg (resolvedRevision == null || builtins.match "[0-9a-f]{40}" resolvedRevision != null) "Build revision must be a full Git SHA.";
  assert lib.assertMsg (revision == null || sourceRevision == "" || revision == sourceRevision) "Build override revision does not match the application flake input.";
  assert lib.assertMsg (builtins.isBool dirty || dirty == null) "Build dirty status must be boolean or null.";
  assert lib.assertMsg (sourceDirty == null || (overrides.dirty or null) == null || overrides.dirty == sourceDirty) "Build override dirty status does not match the application flake input."; {
    inherit dirty;
    revision = resolvedRevision;
    version = "${baseVersion}+${
      if resolvedRevision == null
      then "unknown"
      else "g${resolvedRevision}"
    }${
      if dirty == true
      then ".dirty"
      else if resolvedRevision != null && dirty == null
      then ".unknown"
      else ""
    }";
  }
