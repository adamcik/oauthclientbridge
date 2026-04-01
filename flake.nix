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

    nix2container.url = "github:nlewo/nix2container";
  };

  outputs = {
    nixpkgs,
    uv2nix,
    pyproject-nix,
    pyproject-build-systems,
    nix2container,
    ...
  }: let
    inherit (nixpkgs) lib;
    forAllSystems = lib.genAttrs lib.systems.flakeExposed;

    workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};

    overlay = workspace.mkPyprojectOverlay {
      sourcePreference = "wheel";
    };

    editableOverlay = workspace.mkEditablePyprojectOverlay {
      root = "$REPO_ROOT";
    };

    # Python sets grouped per system
    pythonSets = forAllSystems (
      system: let
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
            passthru =
              old.passthru
              // {
                tests =
                  (old.tests or {})
                  // {
                    pyright = let
                      venv = final.mkVirtualEnv "oauthclientbridge-typing-env" {
                        oauthclientbridge = ["typing"];
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
                          basedpyright src/oauthclientbridge --level error
                        '';
                      };

                    ruff = let
                      venv = final.mkVirtualEnv "oauthclientbridge-lint-env" {
                        oauthclientbridge = ["lint"];
                      };
                    in
                      stdenv.mkDerivation {
                        name = "${final.oauthclientbridge.name}-lint";
                        inherit (final.oauthclientbridge) src;
                        nativeBuildInputs = [
                          venv
                        ];
                        dontConfigure = true;
                        dontInstall = true;
                        buildPhase = ''
                          mkdir $out
                          ruff check
                        '';
                      };

                    # Run pytest with coverage reports installed into build output
                    # TODO: Could this be pytestCheckHook instead?
                    pytest = let
                      venv = final.mkVirtualEnv "oauthclientbridge-pytest-env" {
                        oauthclientbridge = ["test"];
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
  in {
    checks = forAllSystems (
      system: let
        pythonSet = pythonSets.${system};
      in
        # Inherit tests from passthru.tests into flake checks
        pythonSet.oauthclientbridge.passthru.tests
    );

    packages =
      forAllSystems
      (
        system: let
          pkgs = nixpkgs.legacyPackages.${system};
          nix2containerPkgs = nix2container.packages.${system}.nix2container;

          pythonSet = pythonSets.${system};
          runtimeVenv = pythonSet.mkVirtualEnv "oauthclientbridge-runtime-env" {
            oauthclientbridge = ["sentry"];
          };

          depsVenv = pythonSet.mkVirtualEnv "oauthclientbridge-deps-env" (
            pythonSet.oauthclientbridge.dependencies
          );

          python = pkgs.python312;

          uwsgi = pkgs.uwsgi.override {
            python3 = python;
            plugins = ["python3"];
          };

          user = "uwsgi";
          group = "uwsgi";
          uid = "1000";
          gid = "1000";

          shellBin = "/bin/bash";

          mkUser = pkgs.runCommand "mkUser" {} ''
            mkdir -p $out/etc

            cat<<EOF > $out/etc/passwd
            root:x:0:0::/root:${shellBin}
            ${user}:x:${uid}:${gid}::
            EOF

            cat<<EOF > $out/etc/shadow
            root:!x:::::::
            ${user}:!x:::::::
            EOF

            cat<<EOF > $out/etc/group
            root:x:0:0::/root:${shellBin}
            ${user}:x:${toString uid}:${toString gid}::/home/${user}:
            EOF

            cat<<EOF > $out/etc/gshadow
            root:x::
            ${user}:x::
            EOF
          '';

          entrypoint = pkgs.writeScript "entrypoint" ''
            #!${pkgs.stdenv.shell}

            PORT="''${PORT:-8000}"
            WORKERS="''${WORKERS:-$(( $(nproc) * 2 + 1 ))}"
            THREADS="''${THREADS:-2}"

            ${uwsgi}/bin/uwsgi \
              --plugin python3 \
              --module oauthclientbridge.wsgi:app \
              --log-format '%(addr) - %(user) [%(ltime)] "%(method) %(uri) %(proto)" %(status) %(size) "%(referer)" "%(uagent)"' \
              --virtualenv "${runtimeVenv}" \
              --http "0.0.0.0:''${PORT}" \
              --processes "''${WORKERS}" \
              --threads "''${THREADS}" \
              --master \
              --show-config \
              --need-app \
              "$@"
          '';
        in
          lib.optionalAttrs pkgs.stdenv.isLinux {
            # Expose Docker container in packages
            image = nix2containerPkgs.buildImage {
              name = "ghcr.io/adamcik/oauthclientbridge";
              tag = "latest";
              # created = "now";
              config = {
                entrypoint = ["${entrypoint}"];
                user = user;
              };

              layers = let
                baseLayer = nix2containerPkgs.buildLayer {
                  deps = [uwsgi];
                  copyToRoot = [
                    (pkgs.buildEnv {
                      name = "root";
                      paths = with pkgs; [
                        bashInteractive
                        coreutils
                      ];
                      pathsToLink = ["/bin" "/etc" "/run"];
                    })
                    mkUser
                  ];
                };

                depsLayer = nix2containerPkgs.buildLayer {
                  deps = [depsVenv];
                  layers = [baseLayer];
                };

                appLayer = nix2containerPkgs.buildLayer {
                  deps = [runtimeVenv];
                  layers = [
                    baseLayer
                    depsLayer
                  ];
                };
              in [
                baseLayer
                depsLayer
                appLayer
              ];
            };
          }
      );

    # Use an editable Python set for development.
    devShells = forAllSystems (
      system: let
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
                    (old.src + "/src/oauthclientbridge/__init__.py")
                  ];
                };
                nativeBuildInputs =
                  old.nativeBuildInputs
                  ++ final.resolveBuildSystem {
                    editables = [];
                  };
              });
            })
          ]
        );

        venv = editablePythonSet.mkVirtualEnv "oauthclientbridge-dev-env" {
          oauthclientbridge = ["dev"];
        };
      in {
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
