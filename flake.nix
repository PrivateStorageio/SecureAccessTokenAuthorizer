{
  description = "A Tahoe-LAFS storage-system plugin which authorizes storage operations based on privacy-respecting tokens.";
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs?ref=nixos-24.05";
    flake-utils.url = "github:numtide/flake-utils";
    challenge-bypass-ristretto.url = github:LeastAuthority/python-challenge-bypass-ristretto;
    challenge-bypass-ristretto.inputs.nixpkgs.follows = "nixpkgs";
    flake-compat = {
      url = "github:edolstra/flake-compat";
      flake = false;
    };

    # Sometimes it is nice to be able to test against weird versions of some
    # of our dependencies, like arbitrary git revisions or source in local
    # paths.  If we make those dependencies inputs we can override them easily
    # from the command line.
    tahoe-lafs-dev = {
      # More recent versions of Tahoe-LAFS probably provide a flake but we
      # also want to consume older versions which don't, so just treat them
      # all as non-flakes.
      flake = false;
      url = "github:tahoe-lafs/tahoe-lafs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, tahoe-lafs-dev, challenge-bypass-ristretto, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" ] (system: let

      pkgs = nixpkgs.legacyPackages.${system};
      lib = pkgs.lib;

      # The names of the nixpkgs Python derivations for which we will expose
      # packages.
      pyVersions = [ "python310" "python39" ];

      # All of the versions our Tahoe-LAFS dependency for which we will expose
      # packages.
      tahoeVersions = pkgs.python3Packages.callPackage ./nix/tahoe-versions.nix {
        inherit tahoe-lafs-dev;
      };

      # The matrix of package configurations.
      packageCoordinates = lib.attrsets.cartesianProductOfSets {
        pyVersion = pyVersions;
        tahoe-lafs = tahoeVersions;
        challenge-bypass-ristretto = [ (pyVersion: challenge-bypass-ristretto.packages.${system}."${pyVersion}-challenge-bypass-ristretto") ];
      };

      # To avoid being completely overwhelming, for some inputs we only
      # support a single configuration.  Pick that configuration here.
      defaultConfig = builtins.head packageCoordinates;

      # A formatter to construct the appropriate package name for a certain
      # configuration.
      packageName = { pyVersion, tahoe-lafs, challenge-bypass-ristretto }:
        # We only support one version of challenge so we don't bother burning
        # its version into the name.
        "zkapauthorizer-${pyVersion}-tahoe_${tahoe-lafs.version}";

      # Construct a matrix of package-building derivations.
      #
      # data Version = Version { version :: string, buildArgs :: attrset }
      # data Coordinate = Coordinate { pyVersion :: string, tahoe-lafs :: Version }
      #
      # [ Coordinate ] -> { name = derivation; }
      packageMatrix = derivationMatrix packageName packageForVersion;

      # The Hypothesis profiles of the test packages which we will expose.
      hypothesisProfiles = [ "fast" "ci" "big" "default" ];

      # The coverage collection options for the test packages which we will expose.
      coverageOptions = [ false true ];

      # The matrix of test configurations.
      testCoordinates = lib.attrsets.cartesianProductOfSets {
        pyVersion = pyVersions;
        tahoe-lafs = tahoeVersions;
        hypothesisProfile = hypothesisProfiles;
        collectCoverage = coverageOptions;
        challenge-bypass-ristretto = [ (pyVersion: challenge-bypass-ristretto.packages.${system}."${pyVersion}-challenge-bypass-ristretto") ];
      };

      # A formatter to construct the appropriate derivation name for a test
      # configuration.
      testName = { pyVersion, tahoe-lafs, hypothesisProfile, collectCoverage, challenge-bypass-ristretto }:
        builtins.concatStringsSep "-" [
          "tests"
          "${pyVersion}"
          "tahoe_${tahoe-lafs.version}"
          (if hypothesisProfile == null then "default" else hypothesisProfile)
          (if collectCoverage then "cov" else "nocov")
        ];

      # Construct a matrix of test-running derivations.
      #
      # data Coordinate = Coordinate
      #    { pyVersion :: string
      #    , tahoe-lafs :: Version
      #    , hypothesisProfile :: string
      #    , collectCoverage :: bool
      #    }
      #
      # [ Coordinate ] -> { name = derivation; }
      testMatrix = derivationMatrix testName testsForVersion;

      defaultPackageName = packageName defaultConfig;

      inherit (import ./nix/lib.nix {
        inherit pkgs lib;
        src = ./.;
      }) packageForVersion testsForVersion derivationMatrix toWheel;

    in rec {
      devShells = {
        default = pkgs.mkShell {
          # Avoid leaving .pyc all over the source tree when manually
          # triggering tests runs.
          PYTHONDONTWRITEBYTECODE = "1";

          # Make the source for two significant C-language dependencies easily
          # available. Unfortunately, these are the source archives.  Unpack
          # them and use `directory ...` in gdb to help it find them.
          #
          # TODO: Automatically unpack them and provide them as source
          # directories instead.
          SQLITE_SRC = "${pkgs.sqlite.src}";
          PYTHON_SRC = "${pkgs.${defaultConfig.pyVersion}.src}";

          # Make pudb the default.  We make sure it is installed below.
          PYTHONBREAKPOINT = "pudb.set_trace";

          buildInputs = [
            # Put a Python environment that has all of the development, test,
            # and runtime dependencies in it - but not the package itself.
            (pkgs.${defaultConfig.pyVersion}.withPackages (
              ps: with ps;
                [ pudb ]
                ++ self.packages.${system}.default.passthru.lintInputs
                ++ self.packages.${system}.default.passthru.checkInputs
                ++ self.packages.${system}.default.propagatedBuildInputs
            ))

            # Give us gdb in case we need to debug CPython or an extension.
            pkgs.gdb

            # Since we use CircleCI it is handy to have the CircleCI CLI tool
            # available - for example, for validating config changes.
            pkgs.circleci-cli
          ];

          # Add the working copy's package source to the Python environment so
          # we get a convenient way to test against local changes.  Observe
          # that the use of $PWD means this only works if you run `nix
          # develop` from the top of a source checkout.
          shellHook =
            ''
            export PYTHONPATH=$PWD/src
            '';
        };
      };

      packages =
        testMatrix testCoordinates //
        packageMatrix packageCoordinates //
        { default = self.packages.${system}.${defaultPackageName};
          wheel = toWheel self.packages.${system}.default;
        };

      apps = let
        tahoe-env =
          let pkg = self.packages.${system}.default;
          in pkg.passthru.python.withPackages (ps: [ pkg ]);

        checks-env =
          let pkg = self.packages.${system}.default;
          in pkg.passthru.python.withPackages (ps:
            # Put some dependencies useful for different kinds of static
            # checks into the environment.  We ignore `ps` here and take
            # packages from `pkg` instead.  We got `python` from `pkg` too so
            # we know these packages are compatible with the package set we're
            # constructing.

            # Start with the various linting tools, including mypy.
            pkg.passthru.lintInputs

            # mypy requires all of the runtime dependencies in the environment
            # as well
            ++ pkg.propagatedBuildInputs

            # and the test-time dependencies if you want the test suite to
            # type check, too.
            ++ pkg.passthru.checkInputs
          );
        twine-env = pkgs.python3.withPackages (ps: [ ps.twine ]);
      in {
        default = { type = "app"; program = "${tahoe-env}/bin/tahoe"; };
        twine = { type = "app"; program = "${twine-env}/bin/twine"; };
        black = { type = "app"; program = "${checks-env}/bin/black"; };
        isort = { type = "app"; program = "${checks-env}/bin/isort"; };
        flake8 = { type = "app"; program = "${checks-env}/bin/flake8"; };
        mypy = { type = "app"; program = "${checks-env}/bin/mypy"; };
      };
    });
}
