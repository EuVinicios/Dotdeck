# Decisões do Dotdeck (ADRs)

Cada decisão registra o contexto, o que foi decidido e a evidência. Quando a decisão foi do
dono, a resposta literal dele está citada.

---

## ADR-001 — Nome Dotdeck e ID `io.github.euvinicios.Dotdeck` (09/10/2026)

**Contexto.** A missão sugeria `ai.copilotkit.OpenDots`. A Flathub exige que o ID use um
domínio controlado pelo autor (verificação por token em `.well-known` ou DNS), e as regras de
nome e ícone dizem que um app de terceiros "deve ser distinto" e não pode sugerir vínculo
oficial. O protótipo usava o nome "OpenDots" e o mascote da CopilotKit.

**Decisão.** Nome **Dotdeck**, ID `io.github.euvinicios.Dotdeck` (mesmo padrão do PodFlow,
verificado automaticamente pelo login do GitHub), repositório público `EuVinicios/Dotdeck`.

**Resposta do dono:** "Dotdeck (Recomendado)".

---

## ADR-002 — GTK 4 + Libadwaita em Python, não Tauri v2 (09/10/2026)

**Contexto.** A missão pedia para avaliar Tauri v2 (opção A) e GTK 4/Libadwaita (opção B).

**Evidência.**

| Critério | Tauri v2 no Linux | GTK 4 + Libadwaita (Python) |
|---|---|---|
| Motor web | WebKitGTK 4.1 (o mesmo do protótipo) | WebKitGTK 6.0 |
| WebRTC / chamada de voz | `RTCPeerConnection` ausente (mesmo motor) | ausente também → ponte nativa (ADR-003) |
| Visual nativo GNOME | janela GTK 3, sem Libadwaita; onboarding em HTML | `AdwApplicationWindow`, `AdwPreferencesDialog`, `AdwStyleManager` |
| Atalho global no Wayland | plugin só funciona no X11 | portal `GlobalShortcuts` (GNOME 48+, Plasma 6) |
| Flatpak | centenas de crates + node_modules para vender offline | zero módulos extras: tudo vem do runtime GNOME 51 |
| Memória | o processo web do WebKit domina nos dois casos | idem |
| Projeto irmão | — | PodFlow já usa esta pilha, CI e empacotamento |

**Decisão.** Opção B. O "~30 MB de RAM" do Tauri não vale no Linux: o Tauri também usa o
WebKitGTK, e o processo web pesa o mesmo.

---

## ADR-003 — Ponte nativa de WebRTC com GStreamer (09/10/2026)

**Contexto.** As chamadas de voz do OpenDots (`src/client/useVoice.ts`) usam
`RTCPeerConnection` com OpenAI Realtime. Medido nesta máquina:

- WebKitGTK 2.52.6 do Ubuntu 26.04: `typeof RTCPeerConnection === "undefined"`.
- WebKitGTK 2.54.1 do runtime `org.gnome.Platform//51` (Flathub): `"undefined"` também.

Ou seja, a voz não funcionava no protótipo e não funcionaria em Tauri.

**Decisão.** `src/web/rtc-polyfill.js` entrega à página um `RTCPeerConnection` que repassa
cada chamada ao Dotdeck; `src/core/voice.py` faz a conexão real com o `webrtcbin` do
GStreamer (Opus, canal de dados SCTP, DTLS-SRTP, ICE com libnice, cancelamento de eco com
`webrtcdsp`). O áudio não passa pela página: microfone e alto-falante são do GStreamer, e os
botões de mudo da página são repassados. A página do OpenDots não muda nada.

**Prova.** `tests/test_voice.py` e `tests/test_webview.py` fazem uma chamada completa contra um
par GStreamer que se comporta como a OpenAI (oferta sem candidatos, resposta com todos):
conexão, canal aberto, mensagem de ida e volta, áudio remoto, mudo do microfone e do
alto-falante — dentro de um WebKit de verdade.

**Microfone e sandbox.** Não existe um portal `org.freedesktop.portal.Microphone`; no Flatpak o
microfone vem do `--socket=pulseaudio` (PipeWire o atende), no snap da interface
`audio-record`. Por isso o Dotdeck pergunta por servidor ("Usar o microfone?") antes da
primeira chamada e lembra a resposta, como um navegador.

---

## ADR-004 — Token no chaveiro, entregue à página pelo `sessionStorage` (09/10/2026)

**Contexto.** O cliente web do OpenDots lê o token de `sessionStorage["opendots-token"]` e o
manda como `Authorization: Bearer` (`src/client/api.ts`). O protótipo não guardava token.

**Decisão.** O token fica no Secret Service via libsecret (no Flatpak, o portal Secret cuida
disso sem permissão extra; no snap, a interface `password-manager-service`). Um script de
usuário injeta o token no `sessionStorage` antes de qualquer script da página, só no topo e só
na origem exata do servidor (o padrão de URL do WebKit não aceita porta, então o script confere
`location.origin`). **Não existe fallback em texto puro**: sem chaveiro, o token vale só até o
app fechar, e o usuário é avisado.

---

## ADR-005 — Ícone: mascote de pelúcia próprio (09/10/2026)

**Contexto.** O ícone do protótipo era o `public/dots/blue.png` da CopilotKit. Copiar não é
problema de direito autoral (MIT), mas é de marca na Flathub (ADR-001).

**Decisão.** Um mascote próprio no mesmo espírito — bichinho azul, redondo, de pelúcia, com
fones — mas com silhueta, microfone e bochechas próprios. O pelo é renderizado fio a fio
(`tools/render_icon.py`, ~150 mil fios iluminados pela normal 3D), em PNG de 64 a 512 px.

**Respostas do dono:** "Parecido, mas próprio (Recomendado)"; "Quero o ícone com a textura
parecida com a oficial"; "A textura do ícone oficial parece muito mais real e profissional".

---

## ADR-006 — Loja principal: Central de Aplicativos do Ubuntu (Snap Store) (09/10/2026)

**Contexto.** A missão priorizava a Flathub. O dono esclareceu o destino durante o trabalho.

**Decisão.** O snap (`snap/snapcraft.yaml`, base `core26` + extensão `gnome`) é o pacote
principal. O runtime `gnome-core26` já traz GTK 4, Libadwaita, WebKitGTK 6, PyGObject e
GStreamer base/good; o snap embute só os plugins de WebRTC do `plugins-bad` e o `nice`.
O Flatpak continua (outras distribuições e base dos testes) e o `.deb` também.

**Resposta do dono:** "onde quero publicar é na loja oficial do Ubuntu".

---

## ADR-007 — Licenças (09/10/2026)

Código em **GPL-3.0-or-later** (pedido do dono, "mesmo que usamos no projeto PodFlow");
ícones em **CC-BY-SA-4.0**. O Dotdeck não copia código do OpenDots: conversa com ele pela API
HTTP e pela página web, então a licença MIT do OpenDots não se mistura com a do Dotdeck.

---

## ADR-008 — Navegação presa à origem do servidor (09/10/2026)

A janela só carrega páginas da origem do perfil (`esquema://host:porta`). Links para outros
sites, `window.open` e navegações fora da origem abrem no navegador padrão. Assim o token
injetado e a ponte de voz nunca ficam expostos a outra página. TLS inválido é recusado (sem
"aceitar mesmo assim"); a mensagem sugere HTTPS do Tailscale.

---

## ADR-009 — Botão de atualização: atualizador ao lado do servidor (09/10/2026)

**Contexto.** O dono pediu "um botão para o usuário verificar e aplicar atualização quando sair
novidades no projeto oficial". O protótipo antigo fazia `git pull` + build + restart na própria
máquina; isso não funciona dentro do Flatpak/snap nem a partir de outro aparelho (Mac pelo
Tailscale). O OpenDots do homelab tem commits locais que não existem no oficial.

**Decisão.** `server/updater/opendots_updater.py` (só biblioteca padrão do Python + git, npm e
systemctl) roda como serviço de usuário ao lado do OpenDots, na porta 4311, protegido pelo
`OWNER_TOKEN`. O Dotdeck mostra versão, novidades e commits locais e chama `/check` e
`/update`. A atualização faz merge **preservando os commits locais**, `npm ci` só se as
dependências mudaram, build, restart e checagem de saúde; qualquer falha restaura o ponto
anterior. Conflito de merge não muda nada.

**Prova.** `tests/test_updater.py` (7 testes com repositórios git temporários e `npm`/`systemctl`
falsos no PATH) e `tests/test_updates_ui.py` (3 testes da interface com atualizador falso).

## ADR-010 — Backup privado do OpenDots e tokens fora do código (09/10/2026)

**Contexto.** Os commits locais do OpenDots só existiam na máquina; o `origin` é o repositório
da CopilotKit. Ao preparar o backup, `scripts/opendots_telegram_bot.mjs` tinha o token do bot
do Telegram e o `OWNER_TOKEN` escritos no código.

**Decisão.** Repositório privado `EuVinicios/OpenDots` (remoto `backup`). Antes do envio, o
token do Telegram foi para o `.env`, os 4 commits locais foram reescritos sem os tokens e um
commit novo faz o script exigir os dois do `.env`. Cópias (`.env` e bundle do histórico antigo)
em `~/work/backups/opendots-limpeza-<data>/` (local, permissão 700).

**Respostas do dono:** "Sim, fork privado (Recomendado)"; "Limpar antes (Recomendado)".

**Pendente (dono):** trocar os dois tokens, porque já estiveram em texto puro localmente.

## ADR-011 — Voz com a chave do OpenRouter: ponte Realtime + gpt-audio-mini (09/10/2026)

**Contexto.** O OpenDots usa a API Realtime da OpenAI (WebRTC), fixa em `api.openai.com`; o
dono não tem chave da OpenAI, só do OpenRouter. No catálogo do OpenRouter (458 modelos) só
`openai/gpt-audio-mini` e `openai/gpt-audio` respondem em voz, e não há API Realtime: só chat
completions com áudio de entrada e saída (stream obrigatório).

**Medição real (OpenRouter, 09/10/2026).** gpt-audio-mini: primeira fala em ~1,9 s, PCM 24 kHz,
~20 tokens/s de saída e ~10 tokens/s de entrada; US$ 0,00025 por fala de 3,3 s (≈ US$ 0,005
por minuto de fala). Vozes `marin`, `coral` e `nova` funcionam. Ponta a ponta pela ponte: resposta
em português começando 2,7 s após o fim da fala, transcrição correta, US$ 0,00034 no turno.

**Decisão.** `server/voice_gateway` implementa os dois endpoints que o OpenDots usa
(`POST /v1/realtime/calls` e `/hangup`) e os eventos do canal `oai-events`, por turnos: VAD
local, gpt-audio-mini para ouvir/pensar/falar, gemini-2.5-flash-lite para a legenda da fala do
usuário, `ask_compute` repassado como chamada de função. No OpenDots, uma mudança mínima
(`VOICE_REALTIME_URL`, padrão continua a OpenAI) — candidata a PR no projeto oficial.

**Resposta do dono:** "Ponte + gpt-audio-mini (Recomendado)".
