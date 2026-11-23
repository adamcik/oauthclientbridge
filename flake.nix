{
  description = "oauthclientbridge";

  nixConfig = {
    extra-substituters = ["https://oauthclientbridge.cachix.org"];
    extra-trusted-public-keys = ["oauthclientbridge.cachix.org-1:j8sS8Vcj2imJiTxX76XwA1se5oRobp2j/mYgHbP6AKg="];
  };

  inputs = {
    nixpkgs.url = "https://flakehub.com/f/DeterminateSystems/nixpkgs-weekly/0.1";

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
    build-overrides,
    ...
  }: let
    inherit (nixpkgs) lib;
    forAllSystems = lib.genAttrs lib.systems.flakeExposed;

    overrideMetadata = builtins.fromJSON (builtins.readFile build-overrides);
    baseVersion = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
    buildMetadata = import ./nix/build-metadata.nix {
      inherit lib baseVersion;
      source = self;
      overrides = overrideMetadata;
    };
    buildRevision = buildMetadata.revision;

    workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};

    overlay = workspace.mkPyprojectOverlay {
      sourcePreference = "wheel";
    };

    editableOverlay = workspace.mkEditablePyprojectOverlay {
      root = "$REPO_ROOT";
    };

    projectPython = forAllSystems (
      system: nixpkgs.legacyPackages.${system}.python312
    );

    # Python sets grouped per system
    pythonSets = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};

        python = projectPython.${system};

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
              # Basedpyright's bundled Node crashes with newer
              # Nix inputs. Use the Nix-built runtime at the wheel's expected path.
              nodejs-wheel-binaries = prev.nodejs-wheel-binaries.overrideAttrs (old: {
                postFixup =
                  (old.postFixup or "")
                  + ''
                    ln -sf ${pkgs.nodejs_24}/bin/node "$out/${python.sitePackages}/nodejs_wheel/bin/node"
                  '';
              });

              oauthclientbridge = prev.oauthclientbridge.overrideAttrs (old: {
                version = buildMetadata.version;
                __intentionallyOverridingVersion = true;
                # The backend reads project.version; changing the derivation's
                # version alone would leave the installed distribution unstamped.
                postPatch =
                  (old.postPatch or "")
                  + ''
                    substituteInPlace pyproject.toml --replace-fail \
                      ${lib.escapeShellArg "version = ${builtins.toJSON baseVersion}"} \
                      ${lib.escapeShellArg "version = ${builtins.toJSON buildMetadata.version}"}
                  '';
                postInstall =
                  (old.postInstall or "")
                  + ''
                    metadata="$out/${python.sitePackages}/oauthclientbridge-${buildMetadata.version}.dist-info/METADATA"
                    { printf '%s\n' \
                        'Vcs-Revision: ${
                      if buildRevision == null
                      then "unknown"
                      else buildRevision
                    }' \
                        'Vcs-Dirty: ${
                      if buildMetadata.dirty == null
                      then "unknown"
                      else lib.boolToString buildMetadata.dirty
                    }'
                      cat "$metadata"
                    } > "$TMPDIR/METADATA"
                    mv "$TMPDIR/METADATA" "$metadata"
                  '';
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
    nixosModules.default = {pkgs, ...}: {
      imports = [
        (import ./nix/module.nix {appPackages = self.packages.${pkgs.stdenv.hostPlatform.system};})
      ];
    };

    formatter = forAllSystems (system: treefmtEval.${system}.config.build.wrapper);

    checks = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonSet = pythonSets.${system};
        devVenv = pythonSet.mkVirtualEnv "oauthclientbridge-checks-env" {
          oauthclientbridge = ["dev"];
        };
        runtimeVenv = pythonSet.mkVirtualEnv "oauthclientbridge-runtime-env" {
          oauthclientbridge = ["sentry"];
        };
        mkCheck = name: script:
          pkgs.runCommand name {
            src = ./.;
            nativeBuildInputs = [
              devVenv
              pkgs.cacert
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
      in
        {
          lock = mkCheck "uv-lock-check" ''
            uv lock --check
            touch "$out"
          '';

          pyright = mkCheck "pyright-check" ''
            basedpyright src/oauthclientbridge --level error
            touch "$out"
          '';

          ty = mkCheck "ty-check" ''
            ${pkgs.ty}/bin/ty check src/oauthclientbridge --python "$UV_PYTHON" --output-format concise
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

          build-metadata = assert import ./nix/build-metadata-test.nix {inherit lib;};
            pkgs.runCommand "oauthclientbridge-build-metadata-check" {} ''
              touch "$out"
            '';

          packages = pkgs.runCommand "oauthclientbridge-packages-check" {} ''
            export PATH="${runtimeVenv}/bin:$PATH"
            python ${./tests/packaging/check_version.py} ${lib.escapeShellArg (builtins.toJSON buildMetadata)}
            ${self.packages.${system}.management}/bin/oauthclientbridge --help > /dev/null
            ${self.packages.${system}.web}/bin/oauthclientbridge-web --help > /dev/null
            cmp ${./assets/templates/spotify.html} ${self.packages.${system}.assets}/share/oauthclientbridge/templates/spotify.html
            cmp ${./assets/templates/soundcloud.html} ${self.packages.${system}.assets}/share/oauthclientbridge/templates/soundcloud.html
            test -d ${self.packages.${system}.assets}/share/oauthclientbridge/static
            cmp ${./assets/static/robots.txt} ${self.packages.${system}.assets}/share/oauthclientbridge/static/robots.txt
            touch "$out"
          '';
        }
        // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
          module = import ./nix/module-test.nix {
            inherit pkgs;
            appPackages = self.packages.${system};
            module = self.nixosModules.default;
          };
        }
    );

    packages =
      forAllSystems
      (
        system: let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
          app = import ./nix/packages.nix {
            inherit pkgs pythonSet;
          };
        in
          {
            inherit (app) web management assets;
            default = app.web;
          }
          // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
            nixos-permissions-test = import ./nix/permissions-test.nix {inherit pkgs;};
            nixos-test = import ./nix/test.nix {
              inherit pkgs;
              appPackages = self.packages.${system};
              module = self.nixosModules.default;
            };
          }
      );

    # Use an editable Python set for development.
    devShells = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};

        python = projectPython.${system};

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
