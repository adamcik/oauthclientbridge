{
  pkgs,
  appPackages,
  module,
}:
pkgs.testers.runNixOSTest {
  name = "oauthclientbridge";
  nodes.machine = {
    config,
    lib,
    ...
  }: {
    imports = [
      module
      {
        # Check credential delivery for web, explicit migrations, and the job.
        systemd.services =
          lib.genAttrs [
            "oauthclientbridge-alpha"
            "oauthclientbridge-alpha-initdb"
            "oauthclientbridge-alpha-upgradedb"
            "oauthclientbridge-alpha-cleandb"
            "oauthclientbridge-beta"
            "oauthclientbridge-beta-upgradedb"
          ] (_: {
            preStart = ''
              test -r "$CREDENTIALS_DIRECTORY/OAUTH_CLIENT_SECRET"
              test -r "$CREDENTIALS_DIRECTORY/BRIDGE_SESSION_SECRET"
              test -z "''${OAUTH_CLIENT_SECRET+x}"
              test -z "''${BRIDGE_SESSION_SECRET+x}"
            '';
          });
      }
    ];
    virtualisation.memorySize = 2048;
    virtualisation.cores = 2;
    documentation.enable = false;
    environment.systemPackages = [pkgs.curl pkgs.sqlite pkgs.python312 pkgs.util-linux pkgs.iproute2];

    services.oauthclientbridge.instances = {
      alpha = {
        environmentFile = "/run/secrets/alpha";
        credentials = {
          OAUTH_CLIENT_SECRET = "/run/secrets/alpha-client-secret";
          BRIDGE_SESSION_SECRET = "/run/secrets/alpha-session-secret";
        };
        workers = 2;
        cleanDatabase.enable = true;
      };
      beta = {
        environment = {
          OAUTH_CLIENT_ID = "test-beta";
          OAUTH_AUTHORIZATION_URI = "https://provider.example/authorize";
          OAUTH_TOKEN_URI = "https://provider.example/token";
        };
        credentials = {
          OAUTH_CLIENT_SECRET = "/run/secrets/beta-client-secret";
          BRIDGE_SESSION_SECRET = "/run/secrets/beta-session-secret";
        };
        workers = 1;
      };
    };
    # The test controls provisioning order; production units start normally at boot.
    systemd.services.oauthclientbridge-alpha.wantedBy = lib.mkForce [];
    systemd.services.oauthclientbridge-beta.wantedBy = lib.mkForce [];
    systemd.timers.oauthclientbridge-alpha-cleandb = {
      wantedBy = lib.mkForce [];
      timerConfig = {
        # Exercise a real timer without waiting for the production weekly schedule.
        OnActiveSec = "1s";
        RandomizedDelaySec = lib.mkForce 0;
        AccuracySec = "1us";
      };
    };
    systemd.services.upstream = {
      wantedBy = ["multi-user.target"];
      serviceConfig = {
        RuntimeDirectory = "upstream";
        ExecStart = "${pkgs.python312}/bin/python ${../tests/container_smoke/fake_upstream.py} --port-file /run/upstream/port";
      };
    };

    # Proxy and file serving belong to the host, not the application module.
    users.users.nginx.extraGroups = ["oauthclientbridge-alpha"];
    services.nginx = {
      enable = true;
      upstreams.bridge.servers."unix:${config.services.oauthclientbridge.instances.alpha.socketPath}" = {};
      virtualHosts.test = {
        listen = [{addr = "unix:/run/nginx/smoke.sock";}];
        locations."/".proxyPass = "http://bridge";
        locations."= /robots.txt".root = config.services.oauthclientbridge.instances.alpha.staticRoot;
      };
    };
  };

  testScript = ''
    import shlex

    def metrics(name):
        return f"curl --fail --max-time 3 --unix-socket /run/oauthclientbridge-{name}.sock http://localhost/metrics"

    start_all()
    machine.wait_for_unit("upstream.service")
    machine.wait_until_succeeds("test -s /run/upstream/port")
    upstream_port = machine.succeed("cat /run/upstream/port").strip()
    machine.succeed("install -d -m 0700 /run/secrets")
    for name in ("alpha", "beta"):
        # These are fake test credentials, never secret environment assignments.
        machine.succeed(f"printf %s smoke-secret > /run/secrets/{name}-client-secret")
        machine.succeed(f"printf %s test-session-{name} > /run/secrets/{name}-session-secret")
    machine.succeed("chmod 0400 /run/secrets/*")
    environment = "\n".join([
        "OAUTH_CLIENT_ID=smoke-client",
        f"OAUTH_AUTHORIZATION_URI=http://127.0.0.1:{upstream_port}/authorize",
        f"OAUTH_TOKEN_URI=http://127.0.0.1:{upstream_port}/token",
        "OAUTH_REDIRECT_URI=https://auth.example.com/spotify/callback",
        "BRIDGE_SESSION_COOKIE_DOMAIN=auth.example.com",
        "BRIDGE_SESSION_COOKIE_PATH=/spotify",
        "BRIDGE_CALLBACK_TEMPLATE_FILE=/run/test-callback.html",
        # A runtime environment file cannot redirect module-owned writable paths.
        "DB_DATABASE=/tmp/wrong-database",
        "PROMETHEUS_MULTIPROC_DIR=/tmp/wrong-metrics",
    ]) + "\n"
    machine.succeed(f"printf %s {shlex.quote(environment)} > /run/secrets/alpha; chmod 0600 /run/secrets/alpha")
    callback = '{"client_id": {{ client_id|tojson|safe }}, "client_secret": {{ client_secret|tojson|safe }}}'
    machine.succeed(f"printf %s {shlex.quote(callback)} > /run/test-callback.html")

    with subtest("web startup does not initialize or migrate the database"):
        machine.succeed("systemctl start oauthclientbridge-alpha")
        machine.wait_until_succeeds("journalctl -u oauthclientbridge-alpha | grep 'Database must be initialized'")
        machine.succeed("systemctl stop oauthclientbridge-alpha")
        table_count = "SELECT count(*) FROM sqlite_master WHERE name='tokens'"
        assert machine.succeed(f"sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db {shlex.quote(table_count)}").strip() == "0"
        machine.succeed("test ! -e /tmp/wrong-database")

    with subtest("explicit initialization and legacy schema upgrade"):
        machine.succeed("systemctl start oauthclientbridge-alpha-initdb")
        machine.succeed("install -d -m 0700 -o oauthclientbridge-beta -g oauthclientbridge-beta /var/lib/oauthclientbridge-beta")
        legacy = "CREATE TABLE tokens(client_id text primary key, token blob); INSERT INTO tokens VALUES ('legacy', NULL);"
        machine.succeed(f"runuser -u oauthclientbridge-beta -- sqlite3 /var/lib/oauthclientbridge-beta/sqlite.db {shlex.quote(legacy)}")
        machine.succeed("systemctl start oauthclientbridge-beta-upgradedb")
        columns = machine.succeed("sqlite3 /var/lib/oauthclientbridge-beta/sqlite.db 'PRAGMA table_info(tokens)'")
        assert "created_at" in columns and "last_updated_at" in columns
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-beta/sqlite.db 'SELECT client_id FROM tokens'").strip() == "legacy"
        machine.succeed("systemctl start oauthclientbridge-alpha oauthclientbridge-beta")
        machine.wait_until_succeeds(metrics("alpha"))
        machine.wait_until_succeeds(metrics("beta"))

    with subtest("real OAuth exchange through the packaged app and database"):
        machine.wait_for_unit("nginx.service")
        machine.succeed("${pkgs.python312}/bin/python ${../tests/container_smoke/smoke.py} --runtime asgi /run/nginx/smoke.sock")
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-beta/sqlite.db 'SELECT client_id FROM tokens'").strip() == "legacy"

    with subtest("explicit migration stops only its own web instance"):
        machine.succeed("systemctl start oauthclientbridge-alpha-upgradedb")
        machine.fail("systemctl is-active oauthclientbridge-alpha")
        machine.fail("systemctl is-active oauthclientbridge-alpha.socket")
        machine.succeed("test ! -e /run/oauthclientbridge-alpha.sock")
        machine.fail(metrics("alpha"))
        machine.fail("systemctl is-active oauthclientbridge-alpha")
        machine.succeed("systemctl is-active oauthclientbridge-beta")
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
        machine.succeed("systemctl start oauthclientbridge-alpha")
        machine.wait_until_succeeds(metrics("alpha"))

    with subtest("instance identities and writable directories remain isolated"):
        alpha_uid = machine.succeed("id -u oauthclientbridge-alpha").strip()
        beta_uid = machine.succeed("id -u oauthclientbridge-beta").strip()
        assert alpha_uid != beta_uid and alpha_uid != "0" and beta_uid != "0"
        for name, other in (("alpha", "beta"), ("beta", "alpha")):
            machine.succeed(f"test $(stat -c %a /var/lib/oauthclientbridge-{name}) = 700")
            machine.succeed(f"test $(stat -c %a /run/oauthclientbridge-{name}) = 700")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- cat /var/lib/oauthclientbridge-{other}/sqlite.db")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- ls /run/oauthclientbridge-{other}/prom")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- cat /run/secrets/{name}-client-secret")
            machine.succeed(f"test $(stat -c '%a:%U:%G' /run/oauthclientbridge-{name}.sock) = 660:oauthclientbridge-{name}:oauthclientbridge-{name}")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- " + metrics(other))
            pid = machine.succeed(f"systemctl show oauthclientbridge-{name} -p MainPID --value").strip()
            machine.fail(f"ss --tcp --listening --processes --no-header | grep -F 'pid={pid},'")
            # DAC permits this write outside the service; its mount sandbox must not.
            machine.succeed(f"install -d -o oauthclientbridge-{name} /srv/{name}")
            machine.succeed(f"runuser -u oauthclientbridge-{name} -- touch /srv/{name}/outside")
            machine.fail(f"nsenter -t {pid} -m -- runuser -u oauthclientbridge-{name} -- touch /srv/{name}/inside")
            machine.succeed(f"nsenter -t {pid} -m -- runuser -u oauthclientbridge-{name} -- touch /var/lib/oauthclientbridge-{name}/writable")
        machine.succeed("test -n \"$(find /run/oauthclientbridge-alpha/prom -name '*.db' -print -quit)\"")
        machine.succeed("runuser -u nginx -- " + metrics("alpha"))
        machine.fail("runuser -u nginx -- " + metrics("beta"))

    with subtest("credentials stay private and are not exported into process environments"):
        for name, other in (("alpha", "beta"), ("beta", "alpha")):
            unit = f"oauthclientbridge-{name}"
            pid = machine.succeed(f"systemctl show {unit} -p MainPID --value").strip()
            keys = set(machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ | cut -d= -f1").splitlines())
            assert "OAUTH_CLIENT_SECRET" not in keys and "BRIDGE_SESSION_SECRET" not in keys
            directory = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ | grep '^CREDENTIALS_DIRECTORY=' | cut -d= -f2-").strip()
            for key in ("OAUTH_CLIENT_SECRET", "BRIDGE_SESSION_SECRET"):
                path = shlex.quote(f"{directory}/{key}")
                machine.succeed(f"nsenter -t {pid} -m -- runuser -u {unit} -- test -r {path}")
                machine.fail(f"nsenter -t {pid} -m -- runuser -u oauthclientbridge-{other} -- test -r {path}")
            machine.succeed(f"nsenter -t {pid} -m -- cmp {shlex.quote(directory + '/BRIDGE_SESSION_SECRET')} /run/secrets/{name}-session-secret")

    with subtest("cleanup is opt-in and a real timer runs the real command"):
        machine.succeed("systemctl start oauthclientbridge-alpha-cleandb.timer")
        machine.wait_until_succeeds("journalctl -u oauthclientbridge-alpha-cleandb | grep 'Vacuumed /var/lib/oauthclientbridge-alpha/sqlite.db'")
        machine.fail("systemctl cat oauthclientbridge-beta-cleandb.timer")
        machine.fail("systemctl cat oauthclientbridge-beta-cleandb.service")
        for name in ("alpha", "beta"):
            machine.succeed(f"systemctl is-active oauthclientbridge-{name}")

    with subtest("host serves matching static files from the store without Python"):
        machine.succeed("test $(curl --silent --output /dev/null --write-out '%{http_code}' --unix-socket /run/nginx/smoke.sock http://localhost/templates/spotify.html) = 404")
        socket_inode = machine.succeed("stat -c %i /run/oauthclientbridge-alpha.sock").strip()
        machine.succeed("systemctl stop oauthclientbridge-alpha")
        machine.succeed("curl --fail --unix-socket /run/nginx/smoke.sock http://localhost/robots.txt > /run/robots.txt")
        machine.succeed("cmp /run/robots.txt ${appPackages.assets}/share/oauthclientbridge/static/robots.txt")
        machine.fail("systemctl is-active oauthclientbridge-alpha")
        machine.wait_until_succeeds(metrics("alpha"))
        assert machine.succeed("stat -c %i /run/oauthclientbridge-alpha.sock").strip() == socket_inode
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
  '';
}
