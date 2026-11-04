# The flake supplies one matching package set for web, administration, and assets.
{appPackages}: {
  config,
  lib,
  ...
}: let
  inherit (lib) mkOption types;
  cfg = config.services.oauthclientbridge;
  instances = cfg.instances;
  unitName = name: "oauthclientbridge-${name}";
  reserved = ["DB_DATABASE" "PROMETHEUS_MULTIPROC_DIR" "PYTHONDONTWRITEBYTECODE"];
  instanceOptions = {name, ...}: {
    options = {
      environmentFile = mkOption {
        type = types.str;
        description = ''
          Absolute runtime path to a root-owned systemd EnvironmentFile containing
          secrets and provider settings. Do not use a Nix path or put secrets in the store.
        '';
      };
      environment = mkOption {
        type = types.attrsOf types.str;
        default = {};
        description = "Non-secret application environment settings. The runtime file takes precedence.";
      };
      port = mkOption {
        type = types.port;
        default = 8000;
        description = "HTTP port on IPv4 loopback. Each instance must have a distinct port.";
      };
      workers = mkOption {
        type = types.ints.positive;
        default = 4;
        description = "Number of ASGI worker processes.";
      };
      gracefulShutdownTimeout = mkOption {
        type = types.ints.unsigned;
        default = 25;
        description = "Seconds allowed for ASGI graceful shutdown, before systemd's additional margin.";
      };
      cleanDatabase = {
        enable = lib.mkEnableOption "scheduled SQLite vacuuming for this database";
        onCalendar = mkOption {
          type = types.str;
          default = "weekly";
          description = "systemd calendar for the opt-in cleanup job.";
        };
      };
      staticRoot = mkOption {
        type = types.str;
        readOnly = true;
        default = "${appPackages.assets}/share/oauthclientbridge/static";
        description = "Matching public build-time assets for the host's existing file server.";
      };
      stateDirectory = mkOption {
        type = types.str;
        readOnly = true;
        default = "/var/lib/${unitName name}";
        description = "Private persistent instance data, including sqlite.db.";
      };
    };
  };
  instanceServices = name: instance: let
    unit = unitName name;
    # Enforce ownership of writable paths even if the runtime file sets these names.
    environmentScript = ''
      export DB_DATABASE=${lib.escapeShellArg "${instance.stateDirectory}/sqlite.db"}
      export PROMETHEUS_MULTIPROC_DIR="$RUNTIME_DIRECTORY/prom"
      export PYTHONDONTWRITEBYTECODE=1
      mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
    '';
    common = {
      environment = instance.environment;
      serviceConfig = {
        User = unit;
        Group = unit;
        EnvironmentFile = instance.environmentFile;
        StateDirectory = unit;
        StateDirectoryMode = "0700";
        WorkingDirectory = instance.stateDirectory;
        UMask = "0077";
        NoNewPrivileges = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictRealtime = true;
        RestrictNamespaces = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6"];
        SystemCallFilter = ["@system-service"];
        RuntimeDirectoryMode = "0700";
      };
    };
    managementUnit = command:
      lib.recursiveUpdate common {
        description = "OAuth Client Bridge ${name}: explicit ${command}";
        # Starting a migration explicitly stops the web process, never the reverse.
        conflicts = ["${unit}.service"];
        before = ["${unit}.service"];
        script =
          environmentScript
          + ''
            exec ${appPackages.management}/bin/oauthclientbridge ${command}
          '';
        serviceConfig = {
          Type = "oneshot";
          RuntimeDirectory = "${unit}-${command}";
        };
      };
  in
    {
      ${unit} = lib.recursiveUpdate common {
        description = "OAuth Client Bridge (${name})";
        wantedBy = ["multi-user.target"];
        wants = ["network-online.target"];
        after = ["network-online.target"];
        script =
          environmentScript
          + ''
            # Multiprocess metrics must not retain data from an earlier web process.
            rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
            exec ${appPackages.web}/bin/oauthclientbridge-web \
              --host 127.0.0.1 --port ${toString instance.port} \
              --workers ${toString instance.workers} \
              --timeout-graceful-shutdown ${toString instance.gracefulShutdownTimeout}
          '';
        serviceConfig = {
          Type = "simple";
          RuntimeDirectory = unit;
          Restart = "on-failure";
          RestartSec = 5;
          TimeoutStopSec = instance.gracefulShutdownTimeout + 10;
        };
      };
      "${unit}-initdb" = managementUnit "initdb";
      "${unit}-upgradedb" = managementUnit "upgradedb";
    }
    // lib.optionalAttrs instance.cleanDatabase.enable {
      "${unit}-cleandb" = lib.recursiveUpdate common {
        description = "OAuth Client Bridge ${name}: vacuum database";
        script =
          environmentScript
          + ''
            exec ${appPackages.management}/bin/oauthclientbridge cleandb
          '';
        serviceConfig = {
          Type = "oneshot";
          RuntimeDirectory = "${unit}-cleandb";
        };
      };
    };
in {
  options.services.oauthclientbridge.instances = mkOption {
    type = types.attrsOf (types.submodule instanceOptions);
    default = {};
    description = "Isolated bridge instances. Each owns its user, SQLite database, and runtime directories.";
  };

  config = lib.mkIf (instances != {}) {
    assertions =
      [
        {
          assertion = lib.length (lib.unique (map (instance: instance.port) (lib.attrValues instances))) == lib.length (lib.attrNames instances);
          message = "OAuth Client Bridge instances must use distinct loopback ports.";
        }
      ]
      ++ lib.concatLists (lib.mapAttrsToList (name: instance: [
          {
            assertion = builtins.match "[a-z][a-z0-9-]{0,11}" name != null;
            message = "OAuth Client Bridge instance names must be 1–12 lowercase letters, digits, or hyphens, starting with a letter.";
          }
          {
            assertion = lib.hasPrefix "/" instance.environmentFile && !(lib.hasPrefix "/nix/store/" instance.environmentFile);
            message = "OAuth Client Bridge environmentFile must be an absolute runtime path outside the Nix store.";
          }
          {
            assertion = lib.all (key: !(builtins.hasAttr key instance.environment)) reserved;
            message = "OAuth Client Bridge owns DB_DATABASE, PROMETHEUS_MULTIPROC_DIR, and PYTHONDONTWRITEBYTECODE.";
          }
        ])
        instances);

    users.users = lib.mapAttrs' (name: _:
      lib.nameValuePair (unitName name) {
        isSystemUser = true;
        group = unitName name;
      })
    instances;
    users.groups = lib.mapAttrs' (name: _: lib.nameValuePair (unitName name) {}) instances;
    systemd.services = lib.foldl' lib.recursiveUpdate {} (lib.mapAttrsToList instanceServices instances);
    systemd.timers = lib.mapAttrs' (name: instance:
      lib.nameValuePair "${unitName name}-cleandb" {
        wantedBy = ["timers.target"];
        timerConfig = {
          OnCalendar = instance.cleanDatabase.onCalendar;
          Persistent = true;
          RandomizedDelaySec = "15m";
        };
      }) (lib.filterAttrs (_: instance: instance.cleanDatabase.enable) instances);
  };
}
