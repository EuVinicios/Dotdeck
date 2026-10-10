#!/usr/bin/env bash
# Install the Dotdeck server companions next to an OpenDots checkout:
#   - opendots-updater.service       check/apply official updates from Dotdeck (port 4311)
#   - opendots-voice-gateway.service voice calls through OpenRouter (127.0.0.1:4320)
#
# Default is a dry run that only prints what would change. Run with --apply to do it,
# and with --uninstall to undo it (the OpenDots .env is restored from the backup).
#
#   server/install.sh [--apply | --uninstall] [--opendots DIR] [--no-voice] [--no-updater]
#                     [--voice NAME] [--model MODEL]
set -euo pipefail

MODE=dry-run
OPENDOTS="$HOME/github/OpenDots"
WITH_VOICE=1
WITH_UPDATER=1
VOICE_NAME=""
MODEL="openai/gpt-audio-mini"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --uninstall) MODE=uninstall ;;
    --opendots) OPENDOTS="$2"; shift ;;
    --no-voice) WITH_VOICE=0 ;;
    --no-updater) WITH_UPDATER=0 ;;
    --voice) VOICE_NAME="$2"; shift ;;
    --model) MODEL="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; exit 2 ;;
  esac
  shift
done

SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="$HOME/.local/share/dotdeck-server"
CONF="$HOME/.config/dotdeck-server"
UNITS="$HOME/.config/systemd/user"
ENV_FILE="$OPENDOTS/.env"
GATEWAY_ENV="$CONF/voice-gateway.env"
ORIGINAL="$CONF/opendots-voice.original"
VOICE_KEYS="VOICE_REALTIME_URL VOICE_API_KEY VOICE_MODEL VOICE_NAME"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$HOME/work/backups/dotdeck-server-$STAMP"
RUNTIME="org.gnome.Platform//51"

say() { printf '%s\n' "$*"; }
act() {  # act "description" command...
  if [ "$MODE" = apply ] || [ "$MODE" = uninstall ]; then say "• $1"; shift; "$@"; else say "[simulação] $1"; fi
}
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 | sed "s/^[\"']//; s/[\"']\$//"; }
env_set() {  # env_set KEY VALUE  (replaces or appends; the value is never printed)
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^$key=" "$ENV_FILE" > "$tmp" || true
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# -- checks ----------------------------------------------------------------------------
test -d "$OPENDOTS/.git" || { say "OpenDots não encontrado em $OPENDOTS (use --opendots)."; exit 1; }
test -f "$ENV_FILE" || { say "Falta $ENV_FILE."; exit 1; }
for tool in git npm node systemctl python3; do
  command -v "$tool" >/dev/null || { say "Falta o comando $tool."; exit 1; }
done
OWNER_TOKEN="$(env_get OWNER_TOKEN)"
[ "${#OWNER_TOKEN}" -ge 24 ] || { say "OWNER_TOKEN ausente ou curto em $ENV_FILE."; exit 1; }
PORT="$(env_get PORT)"; PORT="${PORT:-4310}"

if [ "$MODE" = uninstall ]; then
  for unit in opendots-voice-gateway.service opendots-updater.service; do
    if [ -f "$UNITS/$unit" ]; then
      act "parar e remover $unit" systemctl --user disable --now "$unit"
      act "apagar $UNITS/$unit" rm -f "$UNITS/$unit"
    fi
  done
  act "recarregar o systemd" systemctl --user daemon-reload
  restore_voice() {
    while IFS='=' read -r key value; do
      [ -n "$key" ] || continue
      if [ "$value" = "__UNSET__" ]; then
        tmp="$(mktemp)"; grep -v "^$key=" "$ENV_FILE" > "$tmp" || true
        chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
      else
        env_set "$key" "$value"
      fi
    done < "$ORIGINAL"
    rm -f "$ORIGINAL"
  }
  if [ -f "$ORIGINAL" ]; then
    act "devolver as configurações de voz originais ao .env do OpenDots" restore_voice
    act "reiniciar o OpenDots" systemctl --user restart opendots.service
  fi
  say "Desinstalado. Os arquivos em $SHARE e $CONF ficam (apague à mão se quiser)."
  exit 0
fi

if [ "$WITH_VOICE" = 1 ]; then
  BASE="$(env_get OPENAI_BASE_URL)"
  KEY="$(env_get OPENAI_API_KEY)"
  case "$BASE" in *openrouter.ai*) ;; *) say "A voz precisa de OPENAI_BASE_URL apontando para o OpenRouter (hoje: ${BASE:-vazio})."; exit 1 ;; esac
  [ -n "$KEY" ] || { say "Falta OPENAI_API_KEY (a chave do OpenRouter) em $ENV_FILE."; exit 1; }
  command -v flatpak >/dev/null && flatpak info --user "$RUNTIME" >/dev/null 2>&1 || {
    say "A ponte de voz roda no runtime $RUNTIME (GStreamer com WebRTC). Instale com:"
    say "  flatpak install --user flathub $RUNTIME"
    exit 1; }
  VOICE_NAME="${VOICE_NAME:-$(env_get VOICE_NAME)}"; VOICE_NAME="${VOICE_NAME:-marin}"
fi

say "OpenDots: $OPENDOTS (porta $PORT)   modo: $MODE"
say "Cópia de segurança: $BACKUP"

# -- files -----------------------------------------------------------------------------
act "criar a cópia de segurança do .env" sh -c "mkdir -p '$BACKUP' && chmod 700 '$BACKUP' && cp -p '$ENV_FILE' '$BACKUP/env.before'"
act "copiar o código para $SHARE" sh -c "mkdir -p '$SHARE' && rm -rf '$SHARE/updater' '$SHARE/voice_gateway' && cp -R '$SRC/updater' '$SRC/voice_gateway' '$SHARE/' && find '$SHARE' -name __pycache__ -prune -exec rm -rf {} +"
act "criar $UNITS" mkdir -p "$UNITS"

if [ "$WITH_UPDATER" = 1 ]; then
  UPDATER_UNIT="[Unit]
Description=OpenDots updater (Dotdeck): official updates with one click
After=network-online.target opendots.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 $SHARE/updater/opendots_updater.py --repo $OPENDOTS --service opendots.service --port 4311
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target"
  act "escrever opendots-updater.service" sh -c "printf '%s\n' \"\$1\" > '$UNITS/opendots-updater.service'" _ "$UPDATER_UNIT"
fi

if [ "$WITH_VOICE" = 1 ]; then
  GATEWAY_TOKEN=""
  [ -f "$GATEWAY_ENV" ] && GATEWAY_TOKEN="$(sed -n 's/^VOICE_GATEWAY_TOKEN=//p' "$GATEWAY_ENV")"
  [ -n "$GATEWAY_TOKEN" ] || GATEWAY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
  write_gateway_env() {
    mkdir -p "$CONF" && chmod 700 "$CONF"
    umask 077
    {
      echo "# Dotdeck voice gateway. The OpenRouter key is a copy of OPENAI_API_KEY from $ENV_FILE."
      echo "VOICE_GATEWAY_TOKEN=$GATEWAY_TOKEN"
      echo "OPENROUTER_API_KEY=$KEY"
      echo "OPENROUTER_BASE_URL=$BASE"
      echo "VOICE_GATEWAY_HOST=127.0.0.1"
      echo "VOICE_GATEWAY_PORT=4320"
      echo "VOICE_GATEWAY_MODEL=$MODEL"
      echo "VOICE_GATEWAY_VOICE=$VOICE_NAME"
    } > "$GATEWAY_ENV"
  }
  act "escrever $GATEWAY_ENV (permissão 600; chave copiada, não exibida)" write_gateway_env
  GATEWAY_UNIT="[Unit]
Description=OpenDots voice gateway (Dotdeck): Realtime-compatible voice calls on OpenRouter
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$GATEWAY_ENV
ExecStart=/usr/bin/flatpak run --no-documents-portal --share=network --filesystem=$SHARE:ro --cwd=$SHARE --command=python3 $RUNTIME -m voice_gateway
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target"
  act "escrever opendots-voice-gateway.service" sh -c "printf '%s\n' \"\$1\" > '$UNITS/opendots-voice-gateway.service'" _ "$GATEWAY_UNIT"
  configure_opendots() {
    # Remember the original voice settings once, so --uninstall can put them back.
    if [ ! -f "$ORIGINAL" ]; then
      mkdir -p "$CONF" && chmod 700 "$CONF"
      ( umask 077
        for key in $VOICE_KEYS; do
          if grep -q "^$key=" "$ENV_FILE"; then printf '%s=%s\n' "$key" "$(env_get "$key")"
          else printf '%s=__UNSET__\n' "$key"; fi
        done > "$ORIGINAL" )
    fi
    env_set VOICE_REALTIME_URL "http://127.0.0.1:4320/v1/realtime/calls"
    env_set VOICE_API_KEY "$GATEWAY_TOKEN"
    env_set VOICE_MODEL "$MODEL"
    env_set VOICE_NAME "$VOICE_NAME"
  }
  act "apontar a voz do OpenDots para a ponte (VOICE_REALTIME_URL, VOICE_API_KEY, VOICE_MODEL=$MODEL, VOICE_NAME=$VOICE_NAME)" configure_opendots
  grep -q "VOICE_REALTIME_URL" "$OPENDOTS/src/server/index.ts" || {
    say "Atenção: este OpenDots ainda não lê VOICE_REALTIME_URL (precisa do commit 'feat(voice): endpoint Realtime configurável')."
    [ "$MODE" = apply ] && exit 1; }
  act "compilar o OpenDots (npm run build)" sh -c "cd '$OPENDOTS' && npm run build >/dev/null"
fi

# -- services --------------------------------------------------------------------------
act "recarregar o systemd" systemctl --user daemon-reload
[ "$WITH_UPDATER" = 1 ] && act "ativar opendots-updater.service" systemctl --user enable --now opendots-updater.service
[ "$WITH_VOICE" = 1 ] && act "ativar opendots-voice-gateway.service" systemctl --user enable --now opendots-voice-gateway.service
[ "$WITH_VOICE" = 1 ] && act "reiniciar o OpenDots" systemctl --user restart opendots.service

[ "$MODE" = apply ] || { say; say "Nada foi alterado. Para aplicar: $0 --apply"; exit 0; }

# -- verification ----------------------------------------------------------------------
say; say "Conferindo…"
ok=1
for i in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER_TOKEN" "http://127.0.0.1:$PORT/api/state" || true)"
  [ "$code" = 200 ] && break; sleep 2
done
[ "$code" = 200 ] && say "✔ OpenDots respondendo" || { say "✘ OpenDots não respondeu (HTTP $code)"; ok=0; }
if [ "$WITH_UPDATER" = 1 ]; then
  sleep 2
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER_TOKEN" http://127.0.0.1:4311/status || true)"
  [ "$code" = 200 ] && say "✔ Atualizador respondendo na porta 4311" || { say "✘ Atualizador não respondeu (HTTP $code)"; ok=0; }
fi
if [ "$WITH_VOICE" = 1 ]; then
  for i in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4320/health || true)"
    [ "$code" = 200 ] && break; sleep 2
  done
  [ "$code" = 200 ] && say "✔ Ponte de voz respondendo em 127.0.0.1:4320" || { say "✘ Ponte de voz não respondeu"; ok=0; }
  voice="$(curl -s -H "Authorization: Bearer $OWNER_TOKEN" "http://127.0.0.1:$PORT/api/workspace" | python3 -c 'import json,sys; print(json.load(sys.stdin)["setup"]["voice"])' 2>/dev/null || echo "?")"
  [ "$voice" = True ] && say "✔ OpenDots com voz configurada" || { say "✘ OpenDots ainda sem voz (setup.voice=$voice)"; ok=0; }
fi
[ "$ok" = 1 ] && say "Tudo pronto." || { say "Algo falhou. Desfazer: $0 --uninstall"; exit 1; }
