#!/usr/bin/env bash
# Install the Dotdeck server companions next to an OpenDots checkout:
#   - opendots-updater.service       check/apply official updates from Dotdeck (127.0.0.1:4311)
#   - opendots-voice-gateway.service voice calls through OpenRouter (127.0.0.1:4320)
#
# Default is a dry run that only prints what would change. Run with --apply to do it,
# and with --uninstall to undo it (the OpenDots .env is restored from the backup).
# --update refreshes the companions already installed (code and unit) and restarts them:
# OpenDots and its .env are left alone (dry run too, unless --apply is also given).
# --updater-host makes the updater listen on another address than 127.0.0.1, for a Dotdeck
# on another device (it then guards OpenDots' rebuilds with the owner token alone).
#
#   server/install.sh [--apply | --uninstall | --update [--apply]] [--opendots DIR]
#                     [--no-voice] [--no-updater] [--voice NAME] [--model MODEL]
#                     [--updater-host ADDRESS]
set -euo pipefail

MODE=dry-run
UPDATE=0
OPENDOTS="$HOME/github/OpenDots"
WITH_VOICE=1
WITH_UPDATER=1
VOICE_NAME=""
MODEL="openai/gpt-audio-mini"
UPDATER_HOST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --opendots|--voice|--model|--updater-host)
      [ $# -ge 2 ] && [ -n "$2" ] || { echo "Falta o valor de $1." >&2; exit 2; } ;;
  esac
  case "$1" in
    --apply) MODE=apply ;;
    --uninstall) MODE=uninstall ;;
    --update) UPDATE=1 ;;
    --opendots) OPENDOTS="$2"; shift ;;
    --no-voice) WITH_VOICE=0 ;;
    --no-updater) WITH_UPDATER=0 ;;
    --voice) VOICE_NAME="$2"; shift ;;
    --model) MODEL="$2"; shift ;;
    --updater-host) UPDATER_HOST="$2"; shift ;;
    -h|--help) sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"; exit 0 ;;
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
GATEWAY_UNIT_FILE="$UNITS/opendots-voice-gateway.service"
UPDATER_UNIT_FILE="$UNITS/opendots-updater.service"
UPDATER_PORT=4311
UPDATER_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/opendots-updater/state.json"
GATEWAY_UNIT="[Unit]
Description=OpenDots voice gateway (Dotdeck): Realtime-compatible voice calls on OpenRouter
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$GATEWAY_ENV
# flatpak moves the sandbox into its own scope, out of this service: --die-with-parent makes
# stopping the service stop the gateway too, and SyslogIdentifier keeps the logs findable
# with journalctl --user -t opendots-voice-gateway
ExecStart=/usr/bin/flatpak run --die-with-parent --no-documents-portal --share=network --filesystem=$SHARE:ro --cwd=$SHARE --command=python3 $RUNTIME -m voice_gateway
SyslogIdentifier=opendots-voice-gateway
# always: a SIGTERM from the session going down (or the OOM killer) is a clean exit for
# systemd, and on-failure left the gateway off until someone started it again
Restart=always
RestartSec=5

[Install]
WantedBy=default.target"

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
write_gateway_unit() { mkdir -p "$UNITS" && printf '%s\n' "$GATEWAY_UNIT" > "$GATEWAY_UNIT_FILE"; }
copy_gateway_code() {  # swap in the new code in one step; the running gateway keeps its copy
  mkdir -p "$SHARE"
  rm -rf "$SHARE/voice_gateway.new"
  cp -R "$SRC/voice_gateway" "$SHARE/voice_gateway.new"
  find "$SHARE/voice_gateway.new" -name __pycache__ -prune -exec rm -rf {} +
  rm -rf "$SHARE/voice_gateway"
  mv "$SHARE/voice_gateway.new" "$SHARE/voice_gateway"
}
stop_stray_gateways() {  # units from Dotdeck 0.2 left the gateway running outside the service
  local pid scope
  for pid in $(pgrep -u "$(id -u)" -f '^python3 -m voice_gateway$' || true); do
    scope="$(sed -n 's|^0::.*/\(app-flatpak-[^/]*\.scope\)$|\1|p' "${PROC_ROOT:-/proc}/$pid/cgroup" 2>/dev/null || true)"
    if [ -n "$scope" ]; then
      systemctl --user stop "$scope" || say "Atenção: não consegui parar $scope (pid $pid)."
    else
      say "Atenção: uma ponte antiga (pid $pid) segue rodando fora do serviço; pare com: kill $pid"
    fi
  done
}
restart_gateway() {  # stop, clear strays, start: the new code is the one answering on :4320
  systemctl --user stop opendots-voice-gateway.service
  stop_stray_gateways
  systemctl --user start opendots-voice-gateway.service
}
backup_gateway() {  # the unit and the code running now, for a rollback (see server/README.md)
  mkdir -p "$BACKUP" && chmod 700 "$BACKUP"
  cp -p "$GATEWAY_UNIT_FILE" "$BACKUP/opendots-voice-gateway.service.before"
  if [ -d "$SHARE/voice_gateway" ]; then
    cp -R "$SHARE/voice_gateway" "$BACKUP/voice_gateway.before"
    find "$BACKUP/voice_gateway.before" -name __pycache__ -prune -exec rm -rf {} +
  fi
}
updater_unit() {  # updater_unit REPO HOST → the unit file
  cat <<EOF
[Unit]
Description=OpenDots updater (Dotdeck): official updates with one click
After=network-online.target opendots.service

[Service]
Type=simple
# --host: the updater rebuilds and restarts OpenDots, so it only answers on this machine
# unless installed with --updater-host (it never follows OpenDots' own HOST).
ExecStart=/usr/bin/python3 $SHARE/updater/opendots_updater.py --repo $1 --service opendots.service --host $2 --port $UPDATER_PORT
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
}
write_updater_unit() { mkdir -p "$UNITS" && updater_unit "$1" "$2" > "$UPDATER_UNIT_FILE"; }
unit_option() {  # unit_option --repo → that option's value in the installed updater unit
  [ -f "$UPDATER_UNIT_FILE" ] && sed -n "s/^ExecStart=.* $1 \([^ ]*\).*/\1/p" "$UPDATER_UNIT_FILE" | head -n1
}
updater_listening() {  # where something listens on the updater's port now ("" = nowhere/unknown)
  command -v ss >/dev/null || return 0
  ss -Hltn "sport = :$UPDATER_PORT" 2>/dev/null | awk '{print $4}' | paste -sd ' ' -
}
updater_busy() {  # true while the updater is applying an OpenDots update
  [ -f "$UPDATER_STATE" ] && python3 -c 'import json, sys
sys.exit(0 if (json.load(open(sys.argv[1])).get("job") or {}).get("state") == "running" else 1)' \
    "$UPDATER_STATE" 2>/dev/null
}
is_loopback() { case "$1" in 127.*|::1|localhost) return 0 ;; *) return 1 ;; esac; }
updater_check_host() {  # the address to reach an updater listening on $1
  case "$1" in 0.0.0.0|::|"") echo 127.0.0.1 ;; *:*) echo "[$1]" ;; *) echo "$1" ;; esac
}
describe_listen() {  # describe_listen HOST → "127.0.0.1:4311 (hoje: 0.0.0.0:4311)"
  local now; now="$(updater_listening)"
  printf '%s:%s (hoje: %s)' "$1" "$UPDATER_PORT" "${now:-não está escutando}"
}
warn_exposed() {
  is_loopback "$1" || say "Atenção: o atualizador vai escutar em $1:$UPDATER_PORT. Qualquer aparelho que alcance esse endereço pode tentar o OWNER_TOKEN; use só numa rede confiável (por exemplo, o IP do Tailscale)."
}
copy_updater_code() {  # same swap as the gateway's
  mkdir -p "$SHARE"
  rm -rf "$SHARE/updater.new"
  cp -R "$SRC/updater" "$SHARE/updater.new"
  find "$SHARE/updater.new" -name __pycache__ -prune -exec rm -rf {} +
  rm -rf "$SHARE/updater"
  mv "$SHARE/updater.new" "$SHARE/updater"
}
backup_updater() {
  mkdir -p "$BACKUP" && chmod 700 "$BACKUP"
  cp -p "$UPDATER_UNIT_FILE" "$BACKUP/opendots-updater.service.before"
  if [ -d "$SHARE/updater" ]; then
    cp -R "$SHARE/updater" "$BACKUP/updater.before"
    find "$BACKUP/updater.before" -name __pycache__ -prune -exec rm -rf {} +
  fi
}
restart_updater() {
  systemctl --user enable opendots-updater.service
  systemctl --user restart opendots-updater.service
}
wait_for_updater() {  # wait_for_updater HOST → prints the /status HTTP status (401 = up, no token)
  local code=000 url="http://$(updater_check_host "$1"):$UPDATER_PORT/status"
  for i in $(seq 1 10); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)"
    case "$code" in 200|401) break ;; esac; sleep 1
  done
  printf '%s' "$code"
}
wait_for_gateway() {  # wait_for_gateway → prints the /health HTTP status
  local code=000
  for i in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4320/health || true)"
    [ "$code" = 200 ] && break; sleep 2
  done
  printf '%s' "$code"
}

# -- update: the companions already installed, never OpenDots --------------------------
if [ "$UPDATE" = 1 ]; then
  [ "$MODE" = uninstall ] && { say "Use --update ou --uninstall, não os dois."; exit 2; }
  for tool in systemctl curl python3 pgrep; do
    command -v "$tool" >/dev/null || { say "Falta o comando $tool."; exit 1; }
  done
  HAS_GATEWAY=0; HAS_UPDATER=0
  [ "$WITH_VOICE" = 1 ] && [ -f "$GATEWAY_UNIT_FILE" ] && [ -f "$GATEWAY_ENV" ] && HAS_GATEWAY=1
  [ "$WITH_UPDATER" = 1 ] && [ -f "$UPDATER_UNIT_FILE" ] && HAS_UPDATER=1
  if [ "$HAS_GATEWAY" = 0 ] && [ "$HAS_UPDATER" = 0 ]; then
    say "Nada para atualizar: a ponte de voz não está instalada aqui (falta $GATEWAY_UNIT_FILE ou $GATEWAY_ENV), nem o atualizador ($UPDATER_UNIT_FILE)."
    say "Instale com: $0 --apply"; exit 1
  fi
  say "Atualizar os serviços do Dotdeck já instalados (o OpenDots e o .env dele não são tocados)   modo: $MODE"
  if [ "$HAS_GATEWAY" = 1 ]; then
    calls="$(curl -s --max-time 3 http://127.0.0.1:4320/health 2>/dev/null | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("calls", 0)))' 2>/dev/null || echo 0)"
    [ "$calls" = 0 ] || { say "Há $calls chamada(s) em andamento; atualize quando terminarem."; exit 1; }
  fi
  if [ "$HAS_UPDATER" = 1 ]; then
    updater_busy && { say "O atualizador está aplicando uma atualização do OpenDots; tente quando terminar."; exit 1; }
    UPDATER_REPO="$(unit_option --repo)"; UPDATER_REPO="${UPDATER_REPO:-$OPENDOTS}"
    UPDATER_HOST="${UPDATER_HOST:-$(unit_option --host)}"; UPDATER_HOST="${UPDATER_HOST:-127.0.0.1}"
  fi
  if [ "$HAS_GATEWAY" = 1 ]; then
    act "guardar o serviço e o código atuais em $BACKUP" backup_gateway
    act "copiar o código novo para $SHARE/voice_gateway" copy_gateway_code
    act "reescrever opendots-voice-gateway.service (logs em opendots-voice-gateway)" write_gateway_unit
  fi
  if [ "$HAS_UPDATER" = 1 ]; then
    warn_exposed "$UPDATER_HOST"
    act "guardar o serviço e o código atuais do atualizador em $BACKUP" backup_updater
    act "copiar o código novo para $SHARE/updater" copy_updater_code
    act "reescrever opendots-updater.service: escuta em $(describe_listen "$UPDATER_HOST"); testes do OpenDots antes de reiniciar" write_updater_unit "$UPDATER_REPO" "$UPDATER_HOST"
  fi
  act "recarregar o systemd" systemctl --user daemon-reload
  [ "$HAS_GATEWAY" = 1 ] && act "reiniciar opendots-voice-gateway.service" restart_gateway
  [ "$HAS_UPDATER" = 1 ] && act "reiniciar opendots-updater.service" systemctl --user restart opendots-updater.service
  [ "$MODE" = apply ] || { say; say "Nada foi alterado. Para aplicar: $0 --update --apply"; exit 0; }
  say; say "Conferindo…"
  ok=1
  if [ "$HAS_GATEWAY" = 1 ]; then
    code="$(wait_for_gateway)"
    if [ "$code" = 200 ]; then
      say "✔ Ponte de voz respondendo em 127.0.0.1:4320"
      say "Logs: journalctl --user -t opendots-voice-gateway -f"
    else
      say "✘ Ponte de voz não respondeu (HTTP $code). Veja: journalctl --user -t opendots-voice-gateway -n 50"; ok=0
    fi
  fi
  if [ "$HAS_UPDATER" = 1 ]; then
    code="$(wait_for_updater "$UPDATER_HOST")"
    case "$code" in
      200|401) say "✔ Atualizador respondendo em $UPDATER_HOST:$UPDATER_PORT" ;;
      *) say "✘ Atualizador não respondeu (HTTP $code). Veja: journalctl --user -u opendots-updater -n 50"; ok=0 ;;
    esac
  fi
  [ "$ok" = 1 ] || exit 1
  exit 0
fi

# -- checks ----------------------------------------------------------------------------
test -d "$OPENDOTS/.git" || { say "OpenDots não encontrado em $OPENDOTS (use --opendots)."; exit 1; }
test -f "$ENV_FILE" || { say "Falta $ENV_FILE."; exit 1; }
for tool in git npm node systemctl python3 pgrep; do
  command -v "$tool" >/dev/null || { say "Falta o comando $tool."; exit 1; }
done
OWNER_TOKEN="$(env_get OWNER_TOKEN)"
[ "${#OWNER_TOKEN}" -ge 24 ] || { say "OWNER_TOKEN ausente ou curto em $ENV_FILE."; exit 1; }
PORT="$(env_get PORT)"; PORT="${PORT:-4310}"
if [ "$WITH_UPDATER" = 1 ] && updater_busy; then
  say "O atualizador está aplicando uma atualização do OpenDots; tente quando terminar."; exit 1
fi

if [ "$MODE" = uninstall ]; then
  for unit in opendots-voice-gateway.service opendots-updater.service; do
    if [ -f "$UNITS/$unit" ]; then
      act "parar e remover $unit" systemctl --user disable --now "$unit"
      act "apagar $UNITS/$unit" rm -f "$UNITS/$unit"
    fi
  done
  act "parar cópias da ponte de voz que ficaram fora do serviço" stop_stray_gateways
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
  UPDATER_HOST="${UPDATER_HOST:-127.0.0.1}"
  warn_exposed "$UPDATER_HOST"
  act "escrever opendots-updater.service: escuta em $(describe_listen "$UPDATER_HOST"); testes do OpenDots antes de reiniciar" write_updater_unit "$OPENDOTS" "$UPDATER_HOST"
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
  act "escrever opendots-voice-gateway.service" write_gateway_unit
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
[ "$WITH_UPDATER" = 1 ] && act "ativar e (re)iniciar opendots-updater.service" restart_updater
[ "$WITH_VOICE" = 1 ] && act "ativar opendots-voice-gateway.service" systemctl --user enable opendots-voice-gateway.service
[ "$WITH_VOICE" = 1 ] && act "(re)iniciar opendots-voice-gateway.service" restart_gateway
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
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $OWNER_TOKEN" "http://$(updater_check_host "$UPDATER_HOST"):$UPDATER_PORT/status" || true)"
  [ "$code" = 200 ] && say "✔ Atualizador respondendo em $UPDATER_HOST:$UPDATER_PORT" || { say "✘ Atualizador não respondeu (HTTP $code)"; ok=0; }
fi
if [ "$WITH_VOICE" = 1 ]; then
  code="$(wait_for_gateway)"
  [ "$code" = 200 ] && say "✔ Ponte de voz respondendo em 127.0.0.1:4320 (logs: journalctl --user -t opendots-voice-gateway -f)" || { say "✘ Ponte de voz não respondeu"; ok=0; }
  voice="$(curl -s -H "Authorization: Bearer $OWNER_TOKEN" "http://127.0.0.1:$PORT/api/workspace" | python3 -c 'import json,sys; print(json.load(sys.stdin)["setup"]["voice"])' 2>/dev/null || echo "?")"
  [ "$voice" = True ] && say "✔ OpenDots com voz configurada" || { say "✘ OpenDots ainda sem voz (setup.voice=$voice)"; ok=0; }
fi
[ "$ok" = 1 ] && say "Tudo pronto." || { say "Algo falhou. Desfazer: $0 --uninstall"; exit 1; }
