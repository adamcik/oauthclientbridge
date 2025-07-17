{
  description = "oauthclientbridge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

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

          python = pkgs.python312;

          # Base Python package set from pyproject.V
          baseSet = pkgs.callPackage pyproject-nix.build.packages {
            inherit python;
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
                        nativeBuildInputs = [
                          venv
                        ];
                        dontConfigure = true;
                        dontInstall = true;
                        buildPhase = ''
                          mkdir $out
                          basedpyright oauthclientbridge --level error
                        '';
                      };

                    # Run pytest with coverage reports installed into build output
                    pytest =
                      let
                        venv = final.mkVirtualEnv "oauthclientbridge-pytest-env" {
                          oauthclientbridge = [ "test" ];
                        };
                      in
                      stdenv.mkDerivation {
                        name = "${final.oauthclientbridge.name}-pytest";
                        inherit (final.oauthclientbridge) src;
                        nativeBuildInputs = [
                          venv
                        ];

                        dontConfigure = true;

                        buildPhase = ''
                          runHook preBuild
                          pytest --cov tests --cov-report html tests
                          runHook postBuild
                        '';

                        installPhase = ''
                          runHook preInstall
                          mv htmlcov $out
                          runHook postInstall
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
          # Expose Docker container in packages
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

          python = pkgs.python312;

          editablePythonSet = pythonSets.${system}.overrideScope (
            lib.composeManyExtensions [
              editableOverlay

              (final: prev: {
                oauthclientbridge = prev.oauthclientbridge.overrideAttrs (old: {
                  src = lib.fileset.toSource {
                    root = old.src;
                    fileset = lib.fileset.unions [
                      (old.src + "/pyproject.toml")
                      (old.src + "/README.md")
                      (old.src + "/oauthclientbridge/__init__.py")
                    ];
                  };
                  nativeBuildInputs =
                    old.nativeBuildInputs
                    ++ final.resolveBuildSystem {
                      editables = [ ];
                    };
                });
              })
            ]
          );

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
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );
    };
}
