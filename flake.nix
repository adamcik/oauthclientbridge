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

    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    nix2container.url = "github:nlewo/nix2container";
  };

  outputs = {
    nixpkgs,
    uv2nix,
    pyproject-nix,
    pyproject-build-systems,
    treefmt-nix,
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

        python = pkgs.python312;

        # Base Python package set from pyproject.V
        baseSet = pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        };
      in
        baseSet.overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
          ]
        )
    );

    treefmtEval = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonSet = pythonSets.${system};
        lintVenv = pythonSet.mkVirtualEnv "oauthclientbridge-lint-env" {
          oauthclientbridge = ["lint"];
        };
      in
        treefmt-nix.lib.evalModule pkgs {
          projectRootFile = "flake.nix";
          programs = {
            alejandra.enable = true;
            actionlint.enable = true;
            zizmor.enable = true;
          };
          settings.formatter = {
            ruff-check = {
              command = "${lintVenv}/bin/ruff";
              includes = ["*.py"];
              options = ["check" "--fix"];
              priority = 10;
            };
            ruff-format = {
              command = "${lintVenv}/bin/ruff";
              includes = ["*.py"];
              options = ["format"];
              priority = 20;
            };
            tombi-format = {
              command = "${pkgs.tombi}/bin/tombi";
              includes = ["*.toml"];
              options = ["format" "--offline"];
            };
            tombi-lint = {
              command = "${pkgs.tombi}/bin/tombi";
              includes = ["*.toml"];
              options = ["lint" "--offline"];
            };
          };
        }
    );
  in {
    formatter = forAllSystems (system: treefmtEval.${system}.config.build.wrapper);

    checks = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonSet = pythonSets.${system};
        devVenv = pythonSet.mkVirtualEnv "oauthclientbridge-checks-env" {
          oauthclientbridge = ["dev"];
        };
        mkCheck = name: script:
          pkgs.runCommand name {
            src = ./.;
            nativeBuildInputs = [
              devVenv
              pkgs.uv
            ];
          } ''
            cd "$src"
            export HOME="$TMPDIR"
            export UV_NO_SYNC="1"
            export UV_PYTHON="${devVenv}/bin/python"
            export UV_PYTHON_DOWNLOADS="never"
            ${script}
          '';
      in {
        lock = mkCheck "uv-lock-check" ''
          uv lock --check
          touch "$out"
        '';

        pyright = mkCheck "pyright-check" ''
          basedpyright src/oauthclientbridge --level error
          touch "$out"
        '';

        pytest = mkCheck "pytest-check" ''
          export COVERAGE_FILE="$TMPDIR/.coverage"
          mkdir -p "$out"
          pytest \
            --basetemp="$TMPDIR/.pytest_basetemp" \
            -o cache_dir="$TMPDIR/.pytest_cache" \
            --cov src/oauthclientbridge \
            --cov-report term-missing:skip-covered \
            --cov-report html:"$TMPDIR/htmlcov" \
            --cov-report xml:"$TMPDIR/coverage.xml" \
            tests
          mv "$TMPDIR/htmlcov" "$out/htmlcov"
          mv "$TMPDIR/coverage.xml" "$out/coverage.xml"
        '';

        treefmt = treefmtEval.${system}.config.build.check ./.;
      }
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
            WORKERS="''${WORKERS:-4}"
            THREADS="''${THREADS:-2}"

            exec ${uwsgi}/bin/uwsgi \
              --plugin python3 \
              --module oauthclientbridge.wsgi:app \
              --disable-logging \
              --virtualenv "${runtimeVenv}" \
              --http "0.0.0.0:''${PORT}" \
              --processes "''${WORKERS}" \
              --threads "''${THREADS}" \
              --master \
              --die-on-term \
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
            pkgs.tombi
            treefmtEval.${system}.config.build.wrapper
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
