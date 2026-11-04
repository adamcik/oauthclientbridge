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
    imports = [module];
    virtualisation.memorySize = 2048;
    virtualisation.cores = 2;
    documentation.enable = false;
    environment.systemPackages = [pkgs.curl pkgs.sqlite pkgs.python312 pkgs.util-linux];

    services.oauthclientbridge.instances = {
      alpha = {
        environmentFile = "/run/secrets/alpha";
        port = 8001;
        workers = 1;
        cleanDatabase.enable = true;
      };
      beta = {
        environmentFile = "/run/secrets/beta";
        port = 8002;
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
    services.nginx = {
      enable = true;
      virtualHosts.test = {
        listen = [{addr = "unix:/run/nginx/smoke.sock";}];
        locations."/".proxyPass = "http://127.0.0.1:8001";
        locations."= /robots.txt".root = config.services.oauthclientbridge.instances.alpha.staticRoot;
      };
    };
  };

  testScript = ''
    import shlex

    start_all()
    machine.wait_for_unit("upstream.service")
    machine.wait_until_succeeds("test -s /run/upstream/port")
    upstream_port = machine.succeed("cat /run/upstream/port").strip()
    machine.succeed("install -d -m 0700 /run/secrets")
    for name in ("alpha", "beta"):
        # These are deliberately fake test credentials, written only at runtime.
        environment = "\n".join([
            "OAUTH_CLIENT_ID=smoke-client",
            "OAUTH_CLIENT_SECRET=smoke-secret",
            f"OAUTH_AUTHORIZATION_URI=http://127.0.0.1:{upstream_port}/authorize",
            f"OAUTH_TOKEN_URI=http://127.0.0.1:{upstream_port}/token",
            "OAUTH_REDIRECT_URI=https://auth.example.com/spotify/callback",
            f"BRIDGE_SESSION_SECRET=test-session-{name}",
            "BRIDGE_SESSION_COOKIE_DOMAIN=auth.example.com",
            "BRIDGE_SESSION_COOKIE_PATH=/spotify",
            "BRIDGE_CALLBACK_TEMPLATE_FILE=/run/test-callback.html",
            # A runtime secret file cannot redirect module-owned writable paths.
            "DB_DATABASE=/tmp/wrong-database",
            "PROMETHEUS_MULTIPROC_DIR=/tmp/wrong-metrics",
        ]) + "\n"
        machine.succeed(f"printf %s {shlex.quote(environment)} > /run/secrets/{name}; chmod 0600 /run/secrets/{name}")
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
        machine.wait_for_open_port(8001)
        machine.wait_for_open_port(8002)
        machine.wait_until_succeeds("curl --fail http://127.0.0.1:8001/metrics")
        machine.wait_until_succeeds("curl --fail http://127.0.0.1:8002/metrics")

    with subtest("real OAuth exchange through the packaged app and database"):
        machine.wait_for_unit("nginx.service")
        machine.succeed("${pkgs.python312}/bin/python ${../tests/container_smoke/smoke.py} --runtime asgi /run/nginx/smoke.sock")
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-beta/sqlite.db 'SELECT client_id FROM tokens'").strip() == "legacy"

    with subtest("explicit migration stops only its own web instance"):
        machine.succeed("systemctl start oauthclientbridge-alpha-upgradedb")
        machine.fail("systemctl is-active oauthclientbridge-alpha")
        machine.succeed("systemctl is-active oauthclientbridge-beta")
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
        machine.succeed("systemctl start oauthclientbridge-alpha")
        machine.wait_until_succeeds("curl --fail http://127.0.0.1:8001/metrics")

    with subtest("instance identities and writable directories remain isolated"):
        alpha_uid = machine.succeed("id -u oauthclientbridge-alpha").strip()
        beta_uid = machine.succeed("id -u oauthclientbridge-beta").strip()
        assert alpha_uid != beta_uid and alpha_uid != "0" and beta_uid != "0"
        for name, other in (("alpha", "beta"), ("beta", "alpha")):
            machine.succeed(f"test $(stat -c %a /var/lib/oauthclientbridge-{name}) = 700")
            machine.succeed(f"test $(stat -c %a /run/oauthclientbridge-{name}) = 700")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- cat /var/lib/oauthclientbridge-{other}/sqlite.db")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- ls /run/oauthclientbridge-{other}/prom")
            machine.fail(f"runuser -u oauthclientbridge-{name} -- cat /run/secrets/{name}")
            pid = machine.succeed(f"systemctl show oauthclientbridge-{name} -p MainPID --value").strip()
            # DAC permits this write outside the service; its mount sandbox must not.
            machine.succeed(f"install -d -o oauthclientbridge-{name} /srv/{name}")
            machine.succeed(f"runuser -u oauthclientbridge-{name} -- touch /srv/{name}/outside")
            machine.fail(f"nsenter -t {pid} -m -- runuser -u oauthclientbridge-{name} -- touch /srv/{name}/inside")
            machine.succeed(f"nsenter -t {pid} -m -- runuser -u oauthclientbridge-{name} -- touch /var/lib/oauthclientbridge-{name}/writable")
        machine.succeed("test -n \"$(find /run/oauthclientbridge-alpha/prom -name '*.db' -print -quit)\"")

    with subtest("cleanup is opt-in and a real timer runs the real command"):
        machine.succeed("systemctl start oauthclientbridge-alpha-cleandb.timer")
        machine.wait_until_succeeds("journalctl -u oauthclientbridge-alpha-cleandb | grep 'Vacuumed /var/lib/oauthclientbridge-alpha/sqlite.db'")
        machine.fail("systemctl cat oauthclientbridge-beta-cleandb.timer")
        machine.fail("systemctl cat oauthclientbridge-beta-cleandb.service")
        for name in ("alpha", "beta"):
            machine.succeed(f"systemctl is-active oauthclientbridge-{name}")

    with subtest("host serves matching static files from the store without Python"):
        machine.succeed("test $(curl --silent --output /dev/null --write-out '%{http_code}' --unix-socket /run/nginx/smoke.sock http://localhost/templates/spotify.html) = 404")
        machine.succeed("systemctl stop oauthclientbridge-alpha")
        machine.succeed("curl --fail --unix-socket /run/nginx/smoke.sock http://localhost/robots.txt > /run/robots.txt")
        machine.succeed("cmp /run/robots.txt ${appPackages.assets}/share/oauthclientbridge/static/robots.txt")
        machine.succeed("systemctl start oauthclientbridge-alpha")
        machine.wait_until_succeeds("curl --fail http://127.0.0.1:8001/metrics")
        assert machine.succeed("sqlite3 /var/lib/oauthclientbridge-alpha/sqlite.db 'SELECT count(*) FROM tokens'").strip() == "1"
  '';
}
