{
  pkgs,
  appPackages,
  module,
}: let
  inherit (pkgs) lib;
  # Distinct store paths catch mixed revisions without building an application.
  overridePackages = lib.genAttrs ["web" "management" "assets"] (
    component: pkgs.runCommand "preprod-${component}" {} "mkdir -p $out"
  );
  evaluated = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit pkgs;
    system = pkgs.stdenv.hostPlatform.system;
    modules = [
      module
      {
        system.stateVersion = "26.05";
        services.oauthclientbridge.instances = {
          alpha.cleanDatabase.enable = true;
          beta = {
            cleanDatabase.enable = true;
            environment = {
              FETCH_VERSIONS = builtins.toJSON ["HTTP/2" "HTTP/1.1"];
              FETCH_MAX_STREAMS = "2";
              FETCH_MAX_ADMISSION_AGE = "3600";
              FETCH_PREPARE_AHEAD = "60";
            };
          };
          preprod = {
            packages = overridePackages;
            cleanDatabase.enable = true;
          };
        };
      }
    ];
  };
  config = evaluated.config;
  checkInstance = name: expected: let
    instance = config.services.oauthclientbridge.instances.${name};
    services = config.systemd.services;
    unit = "oauthclientbridge-${name}";
  in
    lib.assertMsg
    (lib.all (component: instance.packages.${component} == expected.${component}) ["web" "management" "assets"])
    "${name} must select the complete matching package set"
    && lib.assertMsg
    (lib.hasInfix (builtins.unsafeDiscardStringContext "exec ${expected.web}/bin/oauthclientbridge-web ") services.${unit}.script)
    "${name} web must use its selected package"
    && lib.assertMsg
    (lib.all (command:
      lib.hasInfix (builtins.unsafeDiscardStringContext "exec ${expected.management}/bin/oauthclientbridge ${command}")
      services."${unit}-${command}".script) ["initdb" "upgradedb" "cleandb"])
    "${name} management units must use its selected package"
    && lib.assertMsg
    (instance.staticRoot == "${expected.assets}/share/oauthclientbridge/static")
    "${name} staticRoot must use its selected assets";
in
  assert checkInstance "alpha" appPackages;
  assert checkInstance "beta" appPackages;
  assert checkInstance "preprod" overridePackages;
  assert lib.all (command:
    builtins.fromJSON config.systemd.services.${command}.environment.FETCH_VERSIONS
    == ["HTTP/2" "HTTP/1.1"]
    && config.systemd.services.${command}.environment.FETCH_MAX_STREAMS == "2"
    && config.systemd.services.${command}.environment.FETCH_MAX_ADMISSION_AGE == "3600"
    && config.systemd.services.${command}.environment.FETCH_PREPARE_AHEAD == "60")
  ["oauthclientbridge-beta" "oauthclientbridge-beta-initdb" "oauthclientbridge-beta-upgradedb" "oauthclientbridge-beta-cleandb"];
    pkgs.runCommand "oauthclientbridge-module-check" {} "touch $out"
