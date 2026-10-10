# Companheiros de servidor do Dotdeck

Dois serviços pequenos que rodam **ao lado do OpenDots**, no mesmo computador:

| Serviço | Para quê | Onde escuta |
|---|---|---|
| `opendots-updater` | o botão **Atualizar** do Dotdeck: compara o servidor com o OpenDots oficial e aplica as novidades | porta 4311 (mesmo `HOST` do OpenDots), exige o `OWNER_TOKEN` |
| `opendots-voice-gateway` | chamadas de voz com os Dots usando a chave do **OpenRouter** (sem conta na OpenAI) | `127.0.0.1:4320`, só o OpenDots fala com ele |

## Instalar

No servidor, dentro do checkout do Dotdeck:

```bash
server/install.sh --opendots ~/github/OpenDots
```

Isso só **simula** e lista o que faria. Para aplicar:

```bash
server/install.sh --opendots ~/github/OpenDots --apply
```

O instalador:

1. guarda uma cópia do `.env` do OpenDots em `~/work/backups/dotdeck-server-<data>/`;
2. copia o código para `~/.local/share/dotdeck-server/` (os serviços não dependem do checkout);
3. cria os serviços de usuário em `~/.config/systemd/user/`;
4. gera um token só da ponte de voz e escreve `~/.config/dotdeck-server/voice-gateway.env`
   (permissão 600; a chave do OpenRouter é copiada de `OPENAI_API_KEY`, nunca exibida);
5. aponta a voz do OpenDots para a ponte (`VOICE_REALTIME_URL`, `VOICE_API_KEY`,
   `VOICE_MODEL=openai/gpt-audio-mini`, `VOICE_NAME`), compila o OpenDots e o reinicia;
6. confere que OpenDots, atualizador e ponte respondem.

Desfazer tudo (os serviços param e as configurações de voz originais voltam ao `.env`):

```bash
server/install.sh --opendots ~/github/OpenDots --uninstall
```

Opções: `--no-voice`, `--no-updater`, `--voice marin|coral|nova|…`, `--model openai/gpt-audio`.

## Atualizador

Ao clicar em **Atualizar** no Dotdeck (Preferências → Servidores → seu servidor):

1. `git fetch` do projeto oficial (`origin/main`);
2. ponto de restauração em `refs/updater/before-<data>`;
3. `git merge` do oficial **mantendo os commits locais** — se houver conflito, nada muda;
4. `npm ci` (só se `package.json`/`package-lock.json` mudaram) e `npm run build`;
5. `systemctl --user restart opendots.service` e espera o `/api/state` responder;
6. se algo falhar entre 4 e 5, volta ao ponto de restauração, recompila e reinicia;
7. se existir o remoto `backup`, envia a nova versão para ele.

Ele também verifica novidades sozinho a cada 6 horas; o Dotdeck avisa com uma notificação e
uma faixa na janela. Estado e histórico: `~/.local/state/opendots-updater/state.json`.

## Ponte de voz

O OpenDots fala com a ponte como falaria com a API Realtime da OpenAI (WebRTC + eventos no
canal `oai-events`). A ponte detecta quando você para de falar e manda o trecho para o
`openai/gpt-audio-mini` no OpenRouter, que responde em voz; a fala é tocada enquanto chega e
a legenda aparece junto. A ferramenta `ask_compute` (pesquisa pelo Dot) funciona igual.

| Item | Valor medido (09/10/2026) |
|---|---|
| Custo | ~US$ 0,0003 por turno curto; ~US$ 0,005 por minuto de fala do Dot |
| Tempo até começar a responder | ~2,5 s depois que você para de falar |
| Transcrição da sua fala | `google/gemini-2.5-flash-lite` (para legenda e recibo da chamada) |

Diferenças para a Realtime da OpenAI: é por turnos (você fala, ele responde) e a resposta
demora um pouco mais. Você pode interromper falando por cima.

A ponte roda no runtime Flatpak `org.gnome.Platform//51` (GStreamer com WebRTC já incluso),
então não precisa instalar pacotes com `sudo`. Variáveis em `voice-gateway.env`:

| Variável | Padrão |
|---|---|
| `VOICE_GATEWAY_MODEL` | `openai/gpt-audio-mini` (ou `openai/gpt-audio`, ~10× mais caro) |
| `VOICE_GATEWAY_VOICE` | `marin` (também `alloy`, `coral`, `nova`, `shimmer`…) |
| `VOICE_GATEWAY_TRANSCRIBE_MODEL` | `google/gemini-2.5-flash-lite` (vazio desliga) |
| `VOICE_GATEWAY_SILENCE_MS` | `700` (silêncio que encerra a sua fala) |

Logs: `journalctl --user -u opendots-voice-gateway -f` (o custo de cada chamada aparece ao
desligar).

### Acesso de outros aparelhos

O áudio da chamada vai direto do aparelho para a ponte por UDP (o OpenDots só negocia). Na
rede de casa e pelo Tailscale funciona sem configuração; se o servidor tiver firewall, libere
UDP de entrada vindo dessas redes.
