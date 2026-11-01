# 用法：. scripts/larkenv.sh
# 注入与 lark-channel-bridge（默认 profile=claude）一致的 lark-cli 环境；只设置，不 unset。
export LARK_CHANNEL=1
export LARK_CHANNEL_HOME="${LARK_CHANNEL_HOME:-$HOME/.lark-channel}"
export LARK_CHANNEL_PROFILE="${LARK_CHANNEL_PROFILE:-claude}"
export LARK_CHANNEL_CONFIG="$LARK_CHANNEL_HOME/profiles/$LARK_CHANNEL_PROFILE/lark-cli-source/config.json"
export LARKSUITE_CLI_CONFIG_DIR="$LARK_CHANNEL_HOME/profiles/$LARK_CHANNEL_PROFILE/lark-cli"
