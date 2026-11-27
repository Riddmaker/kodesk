{
  description = "kodesk: desktop companion for KOReader e-readers";

  inputs = {
    # Same channel as ~/Git/nixos-config; there, set `kodesk.inputs.nixpkgs.follows = "nixpkgs"`
    # so the app's Qt matches the system Kvantum plugin.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # nixpkgs' PySide6 ships the upstream .pyi stubs but not the PEP 561 `py.typed` marker, so
      # mypy would treat Qt as untyped. Expose just the stubs on MYPYPATH (dev shell only).
      mkPysideStubs =
        pkgs:
        pkgs.runCommand "pyside6-stubs" { } ''
          src=${pkgs.python3Packages.pyside6}/${pkgs.python3.sitePackages}/PySide6
          mkdir -p $out/PySide6
          cp $src/*.pyi $out/PySide6/
          touch $out/PySide6/__init__.pyi $out/PySide6/py.typed
        '';

      # LuaJIT (KOReader's interpreter) with stand-ins for KOReader's json, lfs and SQLite
      # modules: the test suite runs kodesk's reader patch (B-26) with it. Tests only.
      mkLuaForTests = pkgs: pkgs.luajit.withPackages (p: [ p.lua-cjson p.lsqlite3 p.luafilesystem ]);

      mkKodesk =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
          pname = "kodesk";
          version = "0.1.0.dev0";
          pyproject = true;
          src = pkgs.lib.cleanSource ./.;

          build-system = [ pkgs.python3Packages.hatchling ];
          dependencies = [
            pkgs.python3Packages.pyside6
            pkgs.python3Packages.paramiko # Wi-Fi via SFTP (B-11)
          ];

          nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
          buildInputs = [
            pkgs.qt6.qtbase
            pkgs.qt6.qtwayland
            pkgs.qt6.qtsvg
          ];

          # Python entry points are scripts: wrap them via makeWrapper with Qt's env instead.
          dontWrapQtApps = true;
          preFixup = ''
            makeWrapperArgs+=("''${qtWrapperArgs[@]}")
          '';

          postInstall = ''
            install -Dm644 data/io.github.riddmaker.kodesk.desktop -t $out/share/applications
            install -Dm644 data/io.github.riddmaker.kodesk.metainfo.xml -t $out/share/metainfo
            install -Dm644 data/icons/io.github.riddmaker.kodesk.svg -t $out/share/icons/hicolor/scalable/apps
          '';

          nativeCheckInputs = [
            pkgs.python3Packages.pytestCheckHook
            pkgs.python3Packages.pytest-qt
            (mkLuaForTests pkgs)
          ];
          preCheck = ''
            export QT_QPA_PLATFORM=offscreen
            export HOME=$(mktemp -d)
          '';

          meta = {
            description = "Desktop companion for KOReader e-readers";
            homepage = "https://github.com/Riddmaker/kodesk";
            license = pkgs.lib.licenses.gpl3Plus;
            mainProgram = "kodesk";
            platforms = pkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAll (pkgs: rec {
        kodesk = mkKodesk pkgs;
        default = kodesk;
      });

      checks = forAll (pkgs: {
        kodesk = self.packages.${pkgs.stdenv.hostPlatform.system}.kodesk;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (
              ps: with ps; [
                pyside6
                paramiko
                types-paramiko
                hatchling
                pytest
                pytest-qt
                pylint
                mypy
              ]
            ))
            pkgs.ruff
            (mkLuaForTests pkgs) # Lua serializer cross-check, reader patch tests (B-26)
            pkgs.qt6.qtbase
            pkgs.qt6.qtwayland
            pkgs.qt6.qtsvg
            # headless GUI checks (HABIT 7)
            pkgs.sway-unwrapped # no dbus-run-session wrapper: CI runners lack /etc/dbus-1
            pkgs.grim
            pkgs.wtype
            pkgs.desktop-file-utils
            pkgs.appstream
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            export MYPYPATH="${mkPysideStubs pkgs}"
            export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}:${pkgs.qt6.qtwayland}/${pkgs.qt6.qtbase.qtPluginPrefix}:${pkgs.qt6.qtsvg}/${pkgs.qt6.qtbase.qtPluginPrefix}''${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
          '';
        };
      });

      formatter = forAll (pkgs: pkgs.nixfmt-rfc-style);
    };
}
