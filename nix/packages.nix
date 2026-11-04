{
  pkgs,
  pythonSet,
  python,
}: let
  runtime = pythonSet.mkVirtualEnv "oauthclientbridge-runtime-env" {
    oauthclientbridge = ["sentry"];
  };
  dependencies = pythonSet.mkVirtualEnv "oauthclientbridge-deps-env" (
    pythonSet.oauthclientbridge.dependencies
    // pythonSet.oauthclientbridge."dependency-groups".sentry
  );
  uwsgi = pkgs.uwsgi.override {
    python3 = python;
    plugins = ["python3"];
    withPAM = false;
    withSystemd = false;
    withCap = false;
  };
  # Templates contain runtime credentials after rendering; they are not public files.
  assets = pkgs.runCommand "oauthclientbridge-assets" {} ''
    mkdir -p "$out/share/oauthclientbridge/templates" "$out/share/oauthclientbridge/static"
    cp -r ${../deploy/static}/. "$out/share/oauthclientbridge/static/"
    cp ${../deploy/spotify/callback.html} "$out/share/oauthclientbridge/templates/spotify.html"
    cp ${../deploy/soundcloud/callback.html} "$out/share/oauthclientbridge/templates/soundcloud.html"
  '';
  management = pkgs.writeShellScriptBin "oauthclientbridge" ''
    exec ${runtime}/bin/oauthclientbridge "$@"
  '';
  web = pkgs.writeShellScriptBin "oauthclientbridge-web" ''
    exec ${management}/bin/oauthclientbridge serve "$@"
  '';
  wsgi-web = pkgs.writeShellScriptBin "oauthclientbridge-wsgi" ''
    uwsgi_args=(
      --plugin python3
      --module oauthclientbridge.wsgi:app
      --disable-logging
      --log-5xx
      --log-master
      --virtualenv "${runtime}"
      --processes "''${WORKERS:-4}"
      --threads "''${THREADS:-1}"
      --master
      --lazy-app
      --py-call-uwsgi-fork-hooks
      --die-on-term
      --show-config
      --need-app
    )
    exec ${uwsgi}/bin/uwsgi "''${uwsgi_args[@]}" "$@"
  '';
in {
  inherit runtime dependencies uwsgi assets management web wsgi-web;
}
