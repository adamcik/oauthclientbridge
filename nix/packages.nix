{
  pkgs,
  pythonSet,
}: let
  runtime = pythonSet.mkVirtualEnv "oauthclientbridge-runtime-env" {
    oauthclientbridge = ["sentry"];
  };
  dependencies = pythonSet.mkVirtualEnv "oauthclientbridge-deps-env" (
    pythonSet.oauthclientbridge.dependencies
    // pythonSet.oauthclientbridge."dependency-groups".sentry
  );
  # Templates contain runtime credentials after rendering; they are not public files.
  assets = pkgs.runCommand "oauthclientbridge-assets" {} ''
    mkdir -p "$out/share/oauthclientbridge/templates" "$out/share/oauthclientbridge/static"
    cp -r ${../assets/static}/. "$out/share/oauthclientbridge/static/"
    cp ${../assets/templates/spotify.html} "$out/share/oauthclientbridge/templates/spotify.html"
    cp ${../assets/templates/soundcloud.html} "$out/share/oauthclientbridge/templates/soundcloud.html"
  '';
  management = pkgs.writeShellScriptBin "oauthclientbridge" ''
    exec ${runtime}/bin/oauthclientbridge "$@"
  '';
  # Images relocate management to /bin; its original store path does not survive.
  web = pkgs.writeShellScriptBin "oauthclientbridge-web" ''
    exec ${runtime}/bin/oauthclientbridge serve "$@"
  '';
in {
  inherit runtime dependencies assets management web;
}
