# The flake supplies one matching package set for web, administration, and assets.
{appPackages}: {
  config,
  lib,
  pkgs,
  ...
}: let
  inherit (lib) mkOption types;
  cfg = config.services.oauthclientbridge;
  instances = cfg.instances;
  unitName = name: "oauthclientbridge-${name}";
  reserved = ["DB_DATABASE" "PROMETHEUS_MULTIPROC_DIR" "PYTHONDONTWRITEBYTECODE" "CREDENTIALS_DIRECTORY"];
  credentialNames = ["OAUTH_CLIENT_SECRET" "BRIDGE_SESSION_SECRET" "BRIDGE_METRICS_TOKEN" "SENTRY_DSN"];
  isRuntimeFile = path: lib.hasPrefix "/" path && !(lib.hasPrefix "/nix/store/" path);
  instanceOptions = {name, ...}: {
    options = {
      environmentFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Optional absolute runtime path to a root-owned systemd EnvironmentFile.
          Retained for compatibility; prefer credentials for secrets. Do not use
          a Nix path or put secret values in the store.
        '';
      };
      credentials = mkOption {
        type = types.attrsOf types.str;
        default = {};
        description = ''
          Decrypted sops-nix secret paths keyed by ${lib.concatStringsSep ", " credentialNames}.
          systemd loads private copies for web and management units; the application
          reads them without exporting their contents. Use runtime path strings,
          not Nix path literals that copy secrets into the store.
        '';
      };
      environment = mkOption {
        type = types.attrsOf types.str;
        default = {};
        description = "Non-secret application environment settings. The runtime file takes precedence.";
      };
      socketPath = mkOption {
        type = types.str;
        readOnly = true;
        default = "/run/${unitName name}/http.sock";
        description = "HTTP Unix socket, accessible only to the instance user and group.";
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
      databaseAccessUsers = mkOption {
        type = types.listOf types.str;
        default = [];
        description = ''
          Existing trusted users granted read/write SQLite and sidecar access, not
          runtime or credential access. Order backup services after and require
          oauthclientbridge-NAME-database-access.service. Does not configure backups
          or initialize a database. The instance group retains no state access.
        '';
      };
    };
  };
  instanceServices = name: instance: let
    unit = unitName name;
    sharedDatabase = instance.databaseAccessUsers != [];
    databaseUsers = lib.unique ([unit] ++ instance.databaseAccessUsers);
    accessEntries = permissions: lib.concatMapStringsSep "," (user: "u:${user}:${permissions}") databaseUsers;
    databasePermissions = ''
      # Run as the app owner, never as root against app-controlled paths.
      ${pkgs.acl}/bin/setfacl --set ${lib.escapeShellArg "u::rwx,g::---,o::---,${accessEntries "rwx"},m::rwx,d:u::rwx,d:g::---,d:o::---,d:${lib.replaceStrings [","] [",d:"] (accessEntries "rwx")},d:m::rwx"} ${lib.escapeShellArg instance.stateDirectory}
      for file in "$STATE_DIRECTORY/sqlite.db" "$STATE_DIRECTORY/sqlite.db-wal" "$STATE_DIRECTORY/sqlite.db-shm"; do
        test ! -L "$file"
        if test -e "$file"; then
          if test -O "$file"; then
            ${pkgs.acl}/bin/setfacl --set ${lib.escapeShellArg "u::rw-,g::---,o::---,${accessEntries "rw-"},m::rw-"} "$file" || test ! -e "$file"
          else
            # Backup-created sidecars inherit the same ACL, including the app user.
            { test -r "$file" && test -w "$file"; } || test ! -e "$file"
          fi
        fi
      done
    '';
    # Enforce ownership of writable paths even if the runtime file sets these names.
    environmentScript = ''
      export DB_DATABASE=${lib.escapeShellArg "${instance.stateDirectory}/sqlite.db"}
      export PROMETHEUS_MULTIPROC_DIR="$RUNTIME_DIRECTORY/prom"
      export PYTHONDONTWRITEBYTECODE=1
      mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
    '';
    common = {
      environment = instance.environment;
      requires = lib.optional sharedDatabase "${unit}-database-access.service";
      after = lib.optional sharedDatabase "${unit}-database-access.service";
      preStart = lib.optionalString sharedDatabase databasePermissions;
      serviceConfig =
        {
          User = unit;
          Group = unit;
          LoadCredential = lib.mapAttrsToList (key: path: "${key}:${path}") instance.credentials;
          StateDirectory = unit;
          # The access unit establishes g::--- before this mask is widened.
          # Reapplying 0700 here would revoke named-user access on every restart.
          StateDirectoryMode =
            if sharedDatabase
            then "0770"
            else "0700";
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
        }
        // lib.optionalAttrs (instance.environmentFile != null) {
          EnvironmentFile = instance.environmentFile;
        };
    };
    managementUnit = command:
      lib.recursiveUpdate common {
        description = "OAuth Client Bridge ${name}: explicit ${command}";
        # Stop the listener too, so traffic cannot reactivate web during migration.
        conflicts = ["${unit}.service" "${unit}.socket"];
        before = ["${unit}.service" "${unit}.socket"];
        script =
          environmentScript
          + ''
            exec ${appPackages.management}/bin/oauthclientbridge ${command}
          '';
        serviceConfig = {
          Type = "oneshot";
          RuntimeDirectory = "${unit}/private/${command}";
        };
      };
  in
    {
      ${unit} = lib.recursiveUpdate common {
        description = "OAuth Client Bridge (${name})";
        wantedBy = ["multi-user.target"];
        wants = ["network-online.target"];
        requires = common.requires ++ ["${unit}.socket"];
        after = common.after ++ ["network-online.target" "${unit}.socket"];
        script =
          environmentScript
          + ''
            # Multiprocess metrics must not retain data from an earlier web process.
            rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
            exec ${appPackages.web}/bin/oauthclientbridge-web \
              --fd 3 \
              --workers ${toString instance.workers} \
              --timeout-graceful-shutdown ${toString instance.gracefulShutdownTimeout}
          '';
        serviceConfig = {
          Type = "simple";
          RuntimeDirectory = "${unit}/private/web";
          Sockets = ["${unit}.socket"];
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
          RuntimeDirectory = "${unit}/private/cleandb";
        };
      };
    }
    // lib.optionalAttrs sharedDatabase {
      "${unit}-database-access" = {
        description = "OAuth Client Bridge ${name}: database permissions";
        wantedBy = ["multi-user.target"];
        script = databasePermissions;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          User = unit;
          Group = unit;
          StateDirectory = unit;
          StateDirectoryMode = "0700";
          UMask = "0077";
          NoNewPrivileges = true;
          CapabilityBoundingSet = "";
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
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
    assertions = lib.concatLists (lib.mapAttrsToList (name: instance: [
        {
          assertion = builtins.match "[a-z][a-z0-9-]{0,11}" name != null;
          message = "OAuth Client Bridge instance names must be 1–12 lowercase letters, digits, or hyphens, starting with a letter.";
        }
        {
          assertion = instance.environmentFile == null || isRuntimeFile instance.environmentFile;
          message = "OAuth Client Bridge environmentFile must be an absolute runtime path outside the Nix store.";
        }
        {
          assertion = lib.all (key: !(builtins.hasAttr key instance.environment)) reserved;
          message = "OAuth Client Bridge owns ${lib.concatStringsSep ", " reserved}.";
        }
        {
          assertion = lib.all (key: lib.elem key credentialNames) (lib.attrNames instance.credentials);
          message = "OAuth Client Bridge credentials must be ${lib.concatStringsSep ", " credentialNames}.";
        }
        {
          assertion = lib.all isRuntimeFile (lib.attrValues instance.credentials);
          message = "OAuth Client Bridge credential files must be absolute runtime paths outside the Nix store.";
        }
        {
          assertion = lib.all (user: builtins.hasAttr user config.users.users) instance.databaseAccessUsers;
          message = "OAuth Client Bridge databaseAccessUsers must name existing users.";
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
    systemd.tmpfiles.rules = lib.concatLists (lib.mapAttrsToList (name: _: [
        "d /run/${unitName name} 0750 root ${unitName name} -"
        "d /run/${unitName name}/private 0700 ${unitName name} ${unitName name} -"
      ])
      instances);
    systemd.services = lib.foldl' lib.recursiveUpdate {} (lib.mapAttrsToList instanceServices instances);
    systemd.sockets = lib.mapAttrs' (name: instance:
      lib.nameValuePair (unitName name) {
        wantedBy = ["sockets.target"];
        requires = ["systemd-tmpfiles-setup.service"];
        after = ["systemd-tmpfiles-setup.service"];
        socketConfig = {
          ListenStream = instance.socketPath;
          SocketUser = unitName name;
          SocketGroup = unitName name;
          SocketMode = "0660";
          RemoveOnStop = true;
        };
      })
    instances;
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
