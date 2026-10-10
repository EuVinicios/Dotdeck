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

Atualizar só a ponte de voz depois de baixar uma versão nova do Dotdeck (copia o código,
reescreve o serviço e o reinicia; o OpenDots, o `.env` dele e o atualizador não são tocados,
e nada acontece se houver uma chamada em andamento):

```bash
server/install.sh --update            # simula
server/install.sh --update --apply    # aplica
```

Antes de trocar, o `--update` guarda o serviço e o código em uso em
`~/work/backups/dotdeck-server-<data>/`. Para voltar ao código anterior, copie só o código (o
serviço antigo pode trazer de volta o defeito do reinício descrito em [Logs](#logs)):

```bash
B=~/work/backups/dotdeck-server-<data>
rm -rf ~/.local/share/dotdeck-server/voice_gateway
cp -R "$B/voice_gateway.before" ~/.local/share/dotdeck-server/voice_gateway
systemctl --user restart opendots-voice-gateway
```

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

Detalhes que a ponte cuida:

- **Recibo na ordem certa.** A sua fala é transcrita em paralelo com a resposta; antes de
  fechar a legenda do Dot a ponte espera até 1,5 s pela sua transcrição, para o recibo ficar
  "Você → Dot". Se a transcrição atrasar mais que isso, ela é enviada assim que chegar.
- **Nada do que você diz se perde durante uma pesquisa.** Enquanto o Dot espera o resultado
  do `ask_compute`, o que você falar fica numa fila (até 3 falas; acima disso a mais antiga
  sai) e é respondido junto com o resultado.
- **Troca de voz durante a chamada.** Um `session.update` com `audio.output.voice` (uma das
  vozes listadas abaixo) vale a partir da próxima resposta; a ponte confirma com
  `session.updated`. `VOICE_GATEWAY_FORCE_VOICE` tem prioridade sobre tudo.
- **Nenhuma chamada fica presa.** Se o OpenRouter falhar no meio de uma resposta, a ponte avisa
  quem ligou (evento `error`) e continua ouvindo. Uma chamada que não conecta em 60 s ou que
  passa de 20 min (o OpenDots encerra as dele em 15) é encerrada pela ponte, para não ocupar
  uma das vagas de `VOICE_GATEWAY_MAX_CALLS`.

A ponte roda no runtime Flatpak `org.gnome.Platform//51` (GStreamer com WebRTC já incluso),
então não precisa instalar pacotes com `sudo`. Variáveis em `voice-gateway.env`:

| Variável | Padrão |
|---|---|
| `VOICE_GATEWAY_MODEL` | `openai/gpt-audio-mini` (ou `openai/gpt-audio`, ~10× mais caro) |
| `VOICE_GATEWAY_VOICE` | `marin` (também `alloy`, `coral`, `nova`, `shimmer`…) |
| `VOICE_GATEWAY_TRANSCRIBE_MODEL` | `google/gemini-2.5-flash-lite` (vazio desliga) |
| `VOICE_GATEWAY_SILENCE_MS` | `700` (silêncio que encerra a sua fala) |
| `VOICE_GATEWAY_FORCE_VOICE` | vazio (uma voz aqui ignora a que o OpenDots pedir) |
| `VOICE_GATEWAY_MAX_CALLS` | `2` (chamadas ao mesmo tempo) |
| `VOICE_GATEWAY_DEBUG` | vazio (`1` mostra cada turno e o nível do microfone nos logs) |

Vozes: `alloy`, `ash`, `ballad`, `cedar`, `coral`, `echo`, `fable`, `marin`, `nova`, `onyx`,
`sage`, `shimmer`, `verse`.

`GET http://127.0.0.1:4320/health` mostra as chamadas em andamento e os modelos que elas
usam, sem chave nenhuma: `{"ok": true, "calls": 0, "model": "openai/gpt-audio-mini",
"transcribeModel": "google/gemini-2.5-flash-lite"}` (`null` com a transcrição desligada). O
OpenDots lê essa resposta para o Dotdeck mostrar, em **Modelos de IA**, qual modelo fala e
qual transcreve as chamadas. Se o OpenDots pedir um modelo sem `/` (um id da Realtime da
OpenAI), a ponte usa o `VOICE_GATEWAY_MODEL` e registra no log qual modelo atendeu.

### Logs

```bash
journalctl --user -t opendots-voice-gateway -f
```

Use `-t` (o identificador), não `-u`: o serviço roda o Python pelo `flatpak run`, que o move
para um escopo próprio, e por isso `journalctl --user -u opendots-voice-gateway` não mostra
as linhas da ponte. Cada chamada gera uma linha ao começar e outra ao terminar, com duração
e custo; chaves e tokens nunca aparecem:

```text
INFO voice_gateway.http_api: Call rtc_3f… started: model openai/gpt-audio-mini, transcription google/gemini-2.5-flash-lite, voice marin, answered in 0.31 s (1 in progress)
INFO voice_gateway.call: Call rtc_3f… ended (hangup): openai/gpt-audio-mini, 2 min 05 s, 6 replies, 1 compute request, US$ 0.00410
```

Avisos (`WARNING`) aparecem quando o OpenRouter recusa um turno (sem crédito, limite), quando
a transcrição falha e quando o OpenDots usa um token errado — isso acontece se alguém colar
uma chave da OpenAI no campo de voz das configurações do OpenDots, que troca o
`VOICE_API_KEY` do `.env`. Para ver só avisos e erros:

```bash
journalctl --user -t opendots-voice-gateway -p warning
```

Se a ponte não sobe e o log diz `Cannot listen on 127.0.0.1:4320 … Is another voice gateway
still running?`, uma cópia antiga ainda segura a porta (veja o parágrafo abaixo).

Instalações feitas com o Dotdeck 0.2 têm dois defeitos no serviço: os logs ficam em
`journalctl --user -t flatpak -g voice_gateway`, e `systemctl --user restart` não para a ponte
antiga (o `flatpak run` a deixa num escopo próprio, fora do serviço), então a nova não consegue
abrir a porta 4320. `server/install.sh --update --apply` corrige os dois: o serviço passa a usar
`flatpak run --die-with-parent` e a cópia antiga que ficou de fora é parada.

### Acesso de outros aparelhos

O áudio da chamada vai direto do aparelho para a ponte por UDP (o OpenDots só negocia). Na
rede de casa e pelo Tailscale funciona sem configuração; se o servidor tiver firewall, libere
UDP de entrada vindo dessas redes.
