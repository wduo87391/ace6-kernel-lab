{
  description = "OnePlus Ace 6 shared kernel development tools (compile-only)";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.linux = {
    url = "github:torvalds/linux/af32da41b0327b9c6a37856ba82b6760d6c8d10e";
    flake = false;
  };
  outputs = { nixpkgs, linux, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      crossCC = pkgs.pkgsCross.aarch64-multiplatform.pkgsStatic.stdenv.cc;
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [ crossCC pkgs.gcc pkgs.gnumake pkgs.bc pkgs.flex pkgs.bison
          pkgs.perl pkgs.openssl pkgs.python3 pkgs.dtc pkgs.rsync pkgs.patch
          pkgs.git pkgs.coreutils pkgs.ccache ];
        KERNEL_SOURCE = "${linux}";
        CROSS_COMPILE = "${crossCC}/bin/${crossCC.targetPrefix}";
        KERNEL_TOOLCHAIN_ID = "${crossCC}";
        shellHook = ''
          export PYTHONDONTWRITEBYTECODE=1
          # No package is installed from this shell. mkShell's synthetic output
          # flags break wrapper linking when the checkout path contains spaces.
          unset NIX_LDFLAGS NIX_CFLAGS_COMPILE
        '';
      };
    };
}
