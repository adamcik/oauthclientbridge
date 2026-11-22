{
  pkgs,
  pythonSet,
}: let
  runtime = pythonSet.mkVirtualEnv "oauthclientbridge-runtime-env" {
    oauthclientbridge = ["sentry"];
  };
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
  web = pkgs.writeShellScriptBin "oauthclientbridge-web" ''
    exec ${runtime}/bin/oauthclientbridge serve "$@"
  '';
in {
  inherit assets management web;
}
