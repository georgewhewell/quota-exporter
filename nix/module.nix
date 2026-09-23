# Portable NixOS module. The flake's nixosModules.default sets a sensible
# default for `services.llm-quota-exporter.package`. If you vendor this file
# directly, either add the flake's overlay so `pkgs.llm-quota-exporter`
# resolves, or set `services.llm-quota-exporter.package` explicitly.
{ pkgs, config, lib, ... }:

let
  cfg = config.services.llm-quota-exporter;
in
{
  options.services.llm-quota-exporter = {
    enable = lib.mkEnableOption "Prometheus exporter for LLM subscription quotas";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.llm-quota-exporter or (throw "set services.llm-quota-exporter.package");
      defaultText = lib.literalExpression "pkgs.llm-quota-exporter";
      description = "The exporter package to run";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 9184;
      description = "Port to expose the metrics endpoint on";
    };

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Address to bind the metrics endpoint to";
    };

    interval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 300;
      description = "Seconds between polls of the upstream quota endpoints";
    };

    providers = lib.mkOption {
      type = lib.types.str;
      default = "all";
      example = "anthropic,openai";
      description = "Comma-separated provider subset, or \"all\"";
    };

    user = lib.mkOption {
      type = lib.types.str;
      description = ''
        User whose home directory holds the CLI credential files; the service
        runs as this user so it can read (and, for grok/kimi, refresh) them.
      '';
    };

    openaiAccounts = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = { account-a = "/var/lib/codex/account-a"; };
      description = ''
        Named OpenAI accounts mapped to absolute Codex home directories.
        When set, these replace the default OpenAI credential path and export
        provider labels such as openai-account-a, independent of CODEX_HOME.
      '';
    };

    home = lib.mkOption {
      type = lib.types.str;
      default = config.users.users.${cfg.user}.home or "/home/${cfg.user}";
      defaultText = lib.literalExpression "config.users.users.\${cfg.user}.home";
      description = ''
        Home directory containing the credential files. Defaults to the
        declared user's home; set explicitly when the user is not declared in
        the NixOS configuration (e.g. LDAP / mutable users).
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the metrics port in the firewall";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.llm-quota-exporter = {
      description = "LLM subscription quota Prometheus exporter";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = {
        ExecStart = lib.escapeShellArgs ([
          (lib.getExe cfg.package)
          "--listen-address" cfg.listenAddress
          "--port" (toString cfg.port)
          "--interval" (toString cfg.interval)
          "--providers" cfg.providers
          "--home" cfg.home
        ] ++ lib.concatLists (lib.mapAttrsToList (name: path: [
          "--openai-account" "${name}=${path}"
        ]) cfg.openaiAccounts));
        User = cfg.user;
        Group = config.users.users.${cfg.user}.group or "users";
        Restart = "on-failure";
        RestartSec = "30s";
        TimeoutStopSec = "15s";
        # Home is read-only except the credential stores of the providers that
        # rotate refresh tokens (grok, kimi), which must persist rotated pairs.
        ProtectHome = "read-only";
        ReadWritePaths = [
          "-${cfg.home}/.grok"
          "-${cfg.home}/.kimi-code/credentials"
          "-${cfg.home}/.kimi/credentials"
        ];
        ProtectSystem = "strict";
        PrivateTmp = true;
        NoNewPrivileges = true;
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
