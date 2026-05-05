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

    build-overrides = {
      url = "path:./.build-overrides";
      flake = false;
    };
  };

  outputs = {
    self,
    nixpkgs,
    uv2nix,
    pyproject-nix,
    pyproject-build-systems,
    treefmt-nix,
    nix2container,
    build-overrides,
    ...
  }: let
    inherit (nixpkgs) lib;
    forAllSystems = lib.genAttrs lib.systems.flakeExposed;

    overrideMetadata = builtins.fromJSON (builtins.readFile build-overrides);

    fallbackCreated = let
      d = self.lastModifiedDate or "";
    in
      if d == "" || builtins.stringLength d < 14
      then "0001-01-01T00:00:00Z"
      else "${builtins.substring 0 4 d}-${builtins.substring 4 2 d}-${builtins.substring 6 2 d}T${builtins.substring 8 2 d}:${builtins.substring 10 2 d}:${builtins.substring 12 2 d}Z";

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
            (final: prev: {
              oauthclientbridge = prev.oauthclientbridge.overrideAttrs (old: {
                env =
                  (old.env or {})
                  // lib.optionalAttrs ((overrideMetadata.version or null) != null) {
                    SETUPTOOLS_SCM_PRETEND_VERSION = overrideMetadata.version;
                  };
              });
            })
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
            export UV_NO_MANAGED_PYTHON="1"
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
            // pythonSet.oauthclientbridge."dependency-groups".sentry
          );

          python = pkgs.python312;

          uwsgi = pkgs.uwsgi.override {
            python3 = python;
            plugins = ["python3"];
          };

          # Default to a non-root runtime identity; override at run-time when bind-mount
          # ownership needs to match host users (for example: --user 33:33 for www-data).
          uid = "65532";
          gid = "65532";

          runtimeDirs = pkgs.runCommand "oauthclientbridge-runtime-dirs" {} ''
            mkdir -p $out/data
            mkdir -p $out/config
            mkdir -p $out/run/prom
            mkdir -p $out/run/uwsgi
            mkdir -p $out/tmp

            chmod 0777 $out/run/prom
            chmod 0777 $out/run/uwsgi
            chmod 1777 $out/tmp
          '';

          entrypoint = pkgs.writeShellScriptBin "entrypoint" ''
            uwsgi_args=(
              --plugin python3
              --module oauthclientbridge.wsgi:app
              --disable-logging
              --virtualenv "${runtimeVenv}"
              --processes "''${WORKERS:-4}"
              --threads "''${THREADS:-1}"
              --master
              --die-on-term
              --need-app
            )

            exec ${uwsgi}/bin/uwsgi \
              "''${uwsgi_args[@]}" \
              "$@"
          '';
        in
          lib.optionalAttrs pkgs.stdenv.isLinux {
            # Expose Docker container in packages
            image = nix2containerPkgs.buildImage {
              name = "ghcr.io/adamcik/oauthclientbridge";
              tag = "latest";
              created =
                if ((overrideMetadata.created or null) != null)
                then overrideMetadata.created
                else fallbackCreated;

              config = {
                entrypoint = ["/bin/entrypoint"];
                user = "${uid}:${gid}";

                env = [
                  "DB_DATABASE=/data/sqlite.db"
                  "BRIDGE_CALLBACK_TEMPLATE_FILE=/config/callback.html"
                  "PROMETHEUS_MULTIPROC_DIR=/run/prom"
                  "PYTHONDONTWRITEBYTECODE=1"
                ];

                labels = let
                  created =
                    if ((overrideMetadata.created or null) != null)
                    then overrideMetadata.created
                    else fallbackCreated;
                in
                  {
                    "org.opencontainers.image.created" = created;
                    "org.opencontainers.image.description" = "Bridge OAuth2 Authorization Code grants to OAuth2 Client Credentials clients.";
                    "org.opencontainers.image.source" = "https://github.com/adamcik/oauthclientbridge";
                    "org.opencontainers.image.title" = "oauthclientbridge";
                  }
                  // lib.optionalAttrs ((overrideMetadata.revision or null) != null) {
                    "org.opencontainers.image.revision" = overrideMetadata.revision;
                  }
                  // lib.optionalAttrs ((overrideMetadata.version or null) != null) {
                    "org.opencontainers.image.version" = overrideMetadata.version;
                  };
              };

              layers = let
                baseLayer = nix2containerPkgs.buildLayer {
                  deps = [uwsgi];
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

                metadataLayer = nix2containerPkgs.buildLayer {
                  copyToRoot = [
                    entrypoint
                    runtimeDirs
                  ];
                  layers = [
                    baseLayer
                    depsLayer
                    appLayer
                  ];
                };
              in [
                baseLayer
                depsLayer
                appLayer
                metadataLayer
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
            pkgs.actionlint
            venv
            pkgs.tombi
            treefmtEval.${system}.config.build.wrapper
            pkgs.uv
            pkgs.zizmor
          ];
          env = {
            UV_NO_SYNC = "1";
            UV_NO_MANAGED_PYTHON = "1";
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
