{ python ? "3.10"
, doCheck ? true
, backends ? [
    "dask"
    "datafusion"
    "duckdb"
    "pandas"
    "sqlite"
  ]
}:
let
  pkgs = import ./nix;
  drv =
    { poetry2nix
    , python
    , lib
    }:

    let
      buildInputs = with pkgs; [ gdal_2 graphviz-nox proj sqlite ];
      checkInputs = buildInputs;
    in
    poetry2nix.mkPoetryApplication {
      inherit python;

      projectDir = ./.;
      src = pkgs.gitignoreSource ./.;

      overrides = pkgs.poetry2nix.overrides.withDefaults (
        import ./poetry-overrides.nix
      );

      inherit buildInputs checkInputs;

      preCheck = ''
        set -euo pipefail

        tempdir="$(mktemp -d)"

        cp -r ${pkgs.ibisTestingData}/* "$tempdir"

        find "$tempdir" -type f -exec chmod u+rw {} +
        find "$tempdir" -type d -exec chmod u+rwx {} +

        ln -s "$tempdir" ci/ibis-testing-data
      '';

      checkPhase = ''
        set -euo pipefail

        runHook preCheck

        pytest --numprocesses auto --dist loadgroup -m '${lib.concatStringsSep " or " backends} or core'

        runHook postCheck
      '';

      inherit doCheck;

      pythonImportsCheck = [ "ibis" ] ++ (map (backend: "ibis.backends.${backend}") backends);
    };
in
pkgs.callPackage drv {
  python = pkgs."python${builtins.replaceStrings [ "." ] [ "" ] python}";
}
