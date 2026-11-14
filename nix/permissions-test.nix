{pkgs}: let
  # Keep the feedback loop on systemd/DAC, independent of Python app startup.
  appPackages = {
    web = pkgs.writeShellScriptBin "oauthclientbridge-web" ''
      test -S /proc/self/fd/3 || exit 1
      exec ${pkgs.coreutils}/bin/sleep infinity
    '';
    management = pkgs.writeShellScriptBin "oauthclientbridge" "exit 0";
    assets = pkgs.emptyDirectory;
  };
in
  pkgs.testers.runNixOSTest {
    name = "oauthclientbridge-permissions";
    nodes.machine = {lib, ...}: {
      imports = [(import ./module.nix {inherit appPackages;})];
      virtualisation.memorySize = 1024;
      documentation.enable = false;
      environment.systemPackages = [pkgs.sqlite pkgs.python312 pkgs.acl];
      users.users = {
        backup = {
          isSystemUser = true;
          group = "backup";
        };
        proxy = {
          isSystemUser = true;
          group = "proxy";
          extraGroups = ["oauthclientbridge-alpha"];
        };
      };
      users.groups = {
        backup = {};
        proxy = {};
      };
      services.oauthclientbridge.instances = {
        alpha = {
          databaseAccessUsers = ["backup"];
          cleanDatabase.enable = true;
        };
        beta = {};
      };
      systemd.services.oauthclientbridge-alpha.wantedBy = lib.mkForce [];
      systemd.services.oauthclientbridge-beta.wantedBy = lib.mkForce [];
      systemd.timers.oauthclientbridge-alpha-cleandb.wantedBy = lib.mkForce [];
    };
    testScript = ''
      import shlex

      state = "/var/lib/oauthclientbridge-alpha"
      app = "oauthclientbridge-alpha"

      def sqlite_as(user, sql):
          return machine.succeed(f"runuser -u {user} -- sqlite3 {state}/sqlite.db {shlex.quote(sql)}")

      start_all()
      machine.wait_for_unit("multi-user.target")
      machine.wait_for_unit(f"{app}-database-access.service")
      machine.succeed(f"test ! -e {state}/sqlite.db")
      machine.succeed(f"runuser -u backup -- test -w {state}")
      machine.fail(f"runuser -u proxy -- ls {state}")

      with subtest("imported 0600 database gets permissions without content changes"):
          sqlite_as(app, "CREATE TABLE probe(value INTEGER); INSERT INTO probe VALUES(1);")
          machine.succeed(f"chmod 0600 {state}/sqlite.db")
          digest = machine.succeed(f"sha256sum {state}/sqlite.db")
          machine.succeed(f"systemctl restart {app}-database-access")
          assert machine.succeed(f"sha256sum {state}/sqlite.db") == digest
          assert sqlite_as("backup", "SELECT value FROM probe;").strip() == "1"
          machine.succeed(f"chmod 0600 {state}/sqlite.db")
          machine.succeed(f"systemctl start {app}")
          assert machine.succeed(f"sha256sum {state}/sqlite.db") == digest
          assert sqlite_as("backup", "SELECT value FROM probe;").strip() == "1"
          machine.succeed(f"systemctl stop {app}.service {app}.socket")
          sqlite_as("backup", "PRAGMA journal_mode=WAL;")

      with subtest("either identity creates sidecars usable across every app unit restart"):
          for creator, peer in (("backup", app), (app, "backup")):
              holder = "\n".join([
                  "import signal, sqlite3",
                  f"c = sqlite3.connect('{state}/sqlite.db')",
                  "c.execute('INSERT INTO probe VALUES(2)')",
                  "c.commit()",
                  "signal.signal(signal.SIGTERM, lambda *_: exit(0))",
                  "signal.pause()",
              ])
              machine.succeed(f"systemd-run --unit=wal-holder --uid={creator} --property=UMask=0077 ${pkgs.python312}/bin/python -c {shlex.quote(holder)}")
              machine.wait_until_succeeds(f"test -s {state}/sqlite.db-wal && test -s {state}/sqlite.db-shm")
              sqlite_as(peer, "INSERT INTO probe VALUES(3);")
              for command in (f"start {app}", f"restart {app}", f"start {app}-cleandb", f"start {app}-upgradedb", f"start {app}"):
                  machine.succeed(f"systemctl {command}")
                  sqlite_as("backup", "INSERT INTO probe VALUES(4);")
                  sqlite_as(app, "INSERT INTO probe VALUES(5);")
                  for file in ("sqlite.db", "sqlite.db-wal", "sqlite.db-shm"):
                      machine.fail(f"runuser -u proxy -- cat {state}/{file}")
                      machine.fail(f"runuser -u oauthclientbridge-beta -- cat {state}/{file}")
              machine.succeed("systemctl stop wal-holder")
              sqlite_as(app, "PRAGMA wal_checkpoint(TRUNCATE);")
              machine.succeed(f"test ! -e {state}/sqlite.db-wal && test ! -e {state}/sqlite.db-shm")

      with subtest("proxy gets only socket access and default instances stay private"):
          for name in ("alpha", "beta"):
              unit = f"oauthclientbridge-{name}"
              machine.succeed(f"systemctl start {unit}")
              machine.succeed(f"test $(stat -c '%a:%U:%G' /run/{unit}) = 750:root:{unit}")
              machine.succeed(f"test $(stat -c %a /run/{unit}/private/web) = 700")
              machine.fail(f"runuser -u proxy -- ls /run/{unit}/private")
              machine.fail(f"runuser -u backup -- ls /run/{unit}/private")
          connect = "import socket; s = socket.socket(socket.AF_UNIX); s.connect('/run/oauthclientbridge-alpha/http.sock')"
          inode = machine.succeed(f"stat -c %i /run/{app}/http.sock")
          machine.succeed(f"systemctl stop {app}")
          machine.fail(f"systemctl is-active {app}")
          machine.succeed(f"runuser -u proxy -- ${pkgs.python312}/bin/python -c {shlex.quote(connect)}")
          machine.wait_for_unit(f"{app}.service")
          assert machine.succeed(f"stat -c %i /run/{app}/http.sock") == inode
          machine.succeed("test $(stat -c %a /var/lib/oauthclientbridge-beta) = 700")
          machine.fail("runuser -u backup -- ls /var/lib/oauthclientbridge-beta")
          machine.fail("systemctl cat oauthclientbridge-beta-database-access")
    '';
  }
