{
  description = "oauthclientbridge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self
    , nixpkgs
    , uv2nix
    , pyproject-nix
    , pyproject-build-systems
    , ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      wsgiApp = "oauthclientbridge:app";

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      # Python sets grouped per system
      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          inherit (pkgs) stdenv;

          # Base Python package set from pyproject.nix
          baseSet = pkgs.callPackage pyproject-nix.build.packages {
            python = pkgs.python312;
          };

          # An overlay of build fixups & test additions
          pyprojectOverrides = final: prev: {
            oauthclientbridge = prev.oauthclientbridge.overrideAttrs (old: {

              # Add tests to passthru.tests
              #
              # These attribute are used in Flake checks.
              passthru = old.passthru // {
                tests =
                  (old.tests or { })
                    // {

                    pyright =
                      let
                        venv = final.mkVirtualEnv "oauthclientbridge-typing-env" {
                          oauthclientbridge = [ "typing" ];
                        };
                      in
                      stdenv.mkDerivation {
                        name = "${final.oauthclientbridge.name}-typing";
                        inherit (final.oauthclientbridge) src;
                        nativeBuildInputs = [ venv ];
                        dontConfigure = true;
                        dontInstall = true;

                        buildPhase = ''
                          runHook preBuild
                          mkdir $out
                          basedpyright oauthclientbridge --level error
                          runHook postBuild
                        '';
                      };

                    pytest =
                      let
                        venv = final.mkVirtualEnv "oauthclientbridge-pytest-env" {
                          oauthclientbridge = [ "test" ];
                        };
                      in
                      stdenv.mkDerivation {
                        name = "${final.oauthclientbridge.name}-pytest";
                        inherit (final.oauthclientbridge) src;
                        nativeBuildInputs = [ venv ];
                        dontConfigure = true;
                        dontInstall = true;

                        buildPhase = ''
                          runHook preBuild
                          mkdir $out
                          pytest tests
                          runHook postBuild
                        '';
                      };
                  };
              };
            });

          };

        in
        baseSet.overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]
        )
      );

    in
    {
      checks = forAllSystems (
        system:
        let
          pythonSet = pythonSets.${system};
        in
        # Inherit tests from passthru.tests into flake checks
        pythonSet.oauthclientbridge.passthru.tests
      );

      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
        in
        lib.optionalAttrs pkgs.stdenv.isLinux {
          docker =
            let
              venv = pythonSet.mkVirtualEnv "oauthclientbridge-env" workspace.deps.default;
            in
            # FIXME: nix2container
            pkgs.dockerTools.buildLayeredImage {
              name = "oauthclientbridge";
              contents = [ pkgs.cacert ];
              config = {
                # FIXME: Switch to uswgi
                Cmd = [
                  "${venv}/bin/uwsgi"
                ];
              };
            };
        }
      );

      # Use an editable Python set for development.
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          editablePythonSet = pythonSets.${system}.overrideScope editableOverlay;
          venv = editablePythonSet.mkVirtualEnv "oauthclientbridge-dev-env" {
            oauthclientbridge = [ "dev" ];
          };
        in
        {
          default = pkgs.mkShell {
            packages = [
              venv
              pkgs.uv
            ];
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
              export UV_NO_SYNC=1
              export UV_PYTHON_DOWNLOADS=never
            '';
          };
        }
      );
    };
}
