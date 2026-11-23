{lib}: let
  revision = "0123456789012345678901234567890123456789";
  resolve = source: overrides:
    import ./build-metadata.nix {
      inherit lib source overrides;
      baseVersion = "2.1.0";
    };
  rejects = source: overrides:
    !(builtins.tryEval (builtins.deepSeq (resolve source overrides) true)).success;
in
  assert resolve {rev = revision;} {}
  == {
    inherit revision;
    version = "2.1.0+g${revision}";
    dirty = false;
  };
  assert resolve {dirtyRev = "${revision}-dirty";} {dirty = null;}
  == {
    inherit revision;
    version = "2.1.0+g${revision}.dirty";
    dirty = true;
  };
  assert resolve {} {}
  == {
    version = "2.1.0+unknown";
    revision = null;
    dirty = null;
  };
  assert resolve {} {
    inherit revision;
    dirty = false;
  }
  == resolve {rev = revision;} {};
  assert (resolve {} {inherit revision;}).version == "2.1.0+g${revision}.unknown";
  assert rejects {rev = revision;} {revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";};
  assert rejects {dirtyRev = "${revision}-dirty";} {dirty = false;};
  assert rejects {} {revision = "abc1234";};
  assert rejects {} {dirty = "false";};
  assert rejects {rev = revision;} {version = "0.0.0";}; true
