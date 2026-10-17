"""Register the local plugin using this interpreter on Windows or macOS."""
import argparse
import json
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12以降で実行してください。")

import integrations
from runtime_paths import cli_command


def main():
    parser = argparse.ArgumentParser(description="StudioYebisu 日本語エディターをCodexへ組み込む")
    parser.add_argument("--print-config", action="store_true", help="設定例を表示し、登録はしない")
    args = parser.parse_args()
    if args.print_config:
        command = cli_command("mcp")
        result = {"mcpServers": {integrations.SERVER: {"command": command[0], "args": command[1:],
                  "env": integrations.server_environment()}}}
    else:
        result = integrations.install()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except integrations.IntegrationError as exc:
        raise SystemExit(str(exc))
