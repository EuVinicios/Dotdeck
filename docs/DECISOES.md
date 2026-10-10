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

## ADR-012 — Barra lateral nativa e a página do OpenDots em "modo foco" (10/10/2026)

**Contexto.** A barra lateral da página do OpenDots mostrava só 3 dos 8 Dots numa caixa com
rolagem. A causa é uma regra do fork, `.dots-nav { max-height: 200px }` (commit `a576181`); no
projeto oficial a barra não tem limite, mas tem só 220 px e divide espaço com Espaços, conversas
e menus. A referência do dono é o Grok Bot: cada bot é uma linha com nome e função.

**Decisão.** O Dotdeck desenha a própria barra lateral em GTK/Libadwaita
(`src/ui/sidebar.py`): todos os Dots com pet, nome e função, depois os Espaços, e no rodapé
Atividade, Memórias e Configurações. A função vem da primeira linha das instruções
(`# Zelador — SRE da casa`), porque o OpenDots não tem um campo para isso. A página roda em
"modo foco" (`src/web/focus.js`): o script reconhece a página, esconde a barra e o topo dela e
recebe os cliques da barra nativa. Funciona com qualquer servidor OpenDots. Se a página não for
reconhecida (uma versão com outro layout), a barra nativa sai de cena e a página volta a mostrar
a navegação dela, como no 0.2. O modo foco pode ser desligado em Configurações → Aparência.

Os dados vêm de `/api/workspace` e `/api/state`, consultados a cada 4 s com a janela aberta e a
cada 20 s em segundo plano. O OpenDots não tem um canal de eventos; a própria página consulta a
cada 3 s.

**Pedido do dono:** "Veja que a barra lateral onde ficam os dots mostra apenas 3, está tudo muito
apertado e nada profissional como é o GrokBot por exemplo."

## ADR-013 — Pets próprios, um por Dot (10/10/2026)

**Contexto.** A página escolhe o mascote de cada Dot entre só 4 imagens, por um hash do id. Nos
8 Dots do dono, três eram o mesmo pet verde (Dot, Zelador e Consultor), e o desenho aparecia
com 30 px, sem rosto legível.

**Decisão.** São 12 pets de pelúcia próprios (`src/core/pets.py`), desenhados fio a fio pelo
mesmo processo do ícone (`tools/render_pets.py`, 256 px em `src/pets/`). Cada pet tem uma cor e
uma silhueta diferente (fone, broto, orelhas de raposa, antena, coroa, laço, óculos, urso, gorro,
coelho, capelo, capacete), para serem reconhecíveis mesmo pequenos. Os pets são atribuídos
sozinhos pela função do Dot (dinheiro → coroa, homelab → capacete, pesquisa → capelo), sem
repetir enquanto houver pets livres, e a escolha fica salva para não mudar depois. O dono troca
o pet na galeria; se escolher um pet que já é de outro Dot, os dois trocam.

As escolhas ficam neste computador (`~/.config/dotdeck/dots/<servidor>.json`), porque o esquema
dos Dots no OpenDots é estrito e as instruções vão para o prompt do modelo. Dentro da página, as
imagens dos mascotes são trocadas por CSS com `data:` (a CSP do servidor só permite `self` e
`data:`), e assim o mesmo pet aparece na conversa e na chamada. O avatar nativo
(`src/ui/pet.py`) mostra o estado com um anel: trabalhando, em chamada, falando, pausado ou erro,
com animações que respeitam a configuração e o "reduzir movimento" do sistema.

**Pedido do dono:** "Os pets também não estão perfeitos, preciso de algo realmente mais bonito e
fácil de usar."

## ADR-014 — Janela de Configurações avançadas (10/10/2026)

**Contexto.** As Preferências eram um diálogo de 4 abas. As configurações do servidor ficavam
numa janela da página com títulos informais, inglês misturado com português e emoji, e um campo
de chave da OpenAI que, preenchido, quebraria as chamadas pela ponte de voz.

**Decisão.** Uma janela de Configurações própria (`src/ui/settings/`), com seções na lateral
(Aplicativo, Seus Dots, Servidor, Sistema) e busca:
- **Geral, Aparência, Notificações e Atalhos.** As notificações têm uma chave por evento.
- **Dots.** Editor de cada Dot com nome, função, instruções (contador até 2000 caracteres),
  permissões, Espaços e pet.
- **Voz e chamadas.** Escolha de microfone e alto-falante, teste de microfone, eco, ruído e
  aperte-para-falar.
- **Modelo de IA.** Só aparece em servidores que permitem trocar o modelo.
- **Servidores e Privacidade.** Acesso, atualizações, pesquisa e memórias, pausar tudo.
- **Avançado e Sobre.**

O que mexe no servidor e interrompe trabalho em andamento pede confirmação. Chaves nunca são
exibidas nem pré-preenchidas.

**Pedido do dono:** "A tela configurações está muito informal. Quero de fato uma tela de
configuração avançada como app pagos."

## ADR-015 — Ligar para os Dots (10/10/2026)

**Contexto.** O dono perguntou se já dá para usar as chamadas de voz com os bots. Uma chamada
real foi feita pelo caminho do próprio OpenDots (`POST /api/voice/calls` com o SDP, depois
`/active` e `/end`) até a ponte de voz e o `gpt-audio-mini`. O Dot respondeu em português com o
próprio nome 2,7 s depois do fim da fala, a um custo de US$ 0,0005 no turno.

**Decisão.** As chamadas passam a ser um recurso de primeira linha:
- **Onde ligar.** Botão **Ligar** no topo do Dot aberto, no menu de cada Dot, na bandeja
  (Ligar para o Chefe) e em `Ctrl+Shift+L`.
- **Durante a chamada.** O botão vira **Encerrar** com o tempo da chamada, e o Dot em chamada
  ganha um anel verde na barra.
- **Motor da chamada.** Continua sendo o da página (WebRTC pela ponte nativa, ADR-003).
- **Trocar de Dot.** Como trocar de Dot desmonta a chamada da página, o Dotdeck pergunta antes.
- **Sem conversa.** Se o Dot ainda não tem conversa, ela é criada antes de ligar.
- **Botão desligado.** Fica desativado com o motivo quando a voz não está configurada no
  servidor, quando os Dots estão pausados ou quando já há outra chamada.

Os limites são do OpenDots: uma chamada por vez no servidor, até 15 minutos e até 6 pesquisas
por chamada. O encerramento leva cerca de 40 s para salvar o resumo, por isso a interface libera
na hora.

**Pedido do dono:** "avalia se já podemos usar chamadas de voz para fazer as chamadas com os
bots."

## ADR-016 — Publicação direta no canal estável e ficha da loja sem cookies do navegador (10/10/2026)

**Contexto.** O dono pediu para publicar e subir a versão para o canal estável da Snap Store. O
caminho de publicação já existe: o CI gera o snap para amd64 e arm64, publica cada push na `main`
em `edge` e cada tag `v*` em `candidate`. Uma sessão anterior, de outro agente, tinha deixado no
repositório `tools/update_store_listing.py`, que decifrava os cookies do Chrome para editar a
ficha da loja. O script incluía ainda uma chave de reserva fixa, publicada no repositório
público.

**Decisão.**
- **Caminho até o estável.** Mesclar na `main` e marcar a tag `v0.3.0`, o que publica em
  `candidate`. Depois de conferir o `--self-test` do snap publicado, promover para `stable` com
  `snapcraft release`.
- **Ficha da loja.**
  - Título, resumo, descrição, ícone e links vão pelo próprio pacote, com
    `snapcraft upload-metadata`.
  - Capturas e banner são enviados pelo painel do snapcraft.io, na sessão do dono.
  - O script de cookies saiu do repositório. Ler cookies ou senhas do navegador não é aceitável
    para publicar.
- **Capturas.** Saem de um servidor de demonstração (`tools/demo_server.py`, com Dots fictícios
  na interface real do OpenDots), para nunca mostrar os Dots reais do dono.

**Pedido do dono:** "Ao final, publica as novas versões e suba para versão estável na loja",
depois reforçado com "finalizar o aplicativo, publicar e deixar ele na versão stable na
snapcraft".

## ADR-017 — Modelos de IA: provedor, uma chave por provedor, preço e os modelos da chamada (10/10/2026)

**Contexto.** O dono fez duas perguntas sobre o Dotdeck depois de passar a usar só o
OpenDots. A primeira foi se quem baixa o app da loja recebe os Dots e as chaves dele. A
investigação conferiu o snap estável (0.3.2, revisão 20) e o histórico do repositório público,
com os resultados abaixo.
- **Dados do dono.** O pacote não traz Dots, endereços de servidor, tokens nem chaves de API.
  Na primeira execução, quem instala vê a configuração guiada.
- **Onde ficam os dados.** Perfis de servidor ficam na pasta de cada usuário
  (`profiles.json`, modo 600) e os tokens no chaveiro dele. Os Dots vêm do servidor de quem usa.
- **Exceção.** O único resto pessoal no código era um exemplo de docstring com o nome do dono.

A segunda pergunta foi sobre provedores e modelos. A página **Modelo de IA** mostrava o id
cru (`deepseek/deepseek-v4-flash-0731`), uma chave só, nenhum preço e nenhum jeito de trocar
de provedor. A página de voz mostrava só um dos três modelos de uma chamada: o que fala. O que
transcreve e o que pesquisa durante a chamada (o modelo das conversas) não apareciam. A chave
vazia aparecia como "Não definido", no masculino.

**Decisão.**
- **Loja sem dados de ninguém.** Isso continua valendo e agora é conferido na CI:
  `make privacy` (`tools/privacy_scan.py`) procura chaves (`sk-or-`, `sk-ant-`, `sk-proj-`),
  tokens de bot do Telegram, pastas `/home/<nome>` e endereços privados ou do Tailscale. A busca
  roda no código a cada push e, no fluxo do snap, em todos os arquivos do pacote gerado. Os
  padrões são genéricos, nunca os valores reais do dono. Os exemplos dos textos de ajuda
  (192.168.0.10, localhost, `tail1234`) são permitidos. O exemplo com o nome do dono virou um
  exemplo fictício.
- **Página Modelos de IA**, em quatro grupos:
  - **Modelo das conversas.** Nome amigável, com id e provedor embaixo, custo aproximado
    ("US$ 0,10 entrada · US$ 0,50 saída por milhão de tokens"), chave do provedor
    (Configurada ou Nenhuma chave ainda) e **Testar agora** ("Respondeu em 1,2 s" ou o erro).
  - **Trocar.** Provedor, depois o modelo daquele provedor (cada item com preço e
    velocidade), **Personalizado…** e, só para "Compatível com OpenAI", o endereço da API em
    **Avançado**.
  - **Chaves de API.** Uma linha por provedor, com o estado e **Definir nova chave**.
  - **Modelo das chamadas de voz.** Fala, Transcrição e Pesquisas durante a chamada, com o
    botão **Configurar chamadas…**.
- **Provedores.** OpenRouter, Anthropic com chave de API e "Compatível com OpenAI". O Haiku 5.5
  chega pelo OpenRouter (`anthropic/claude-haiku-5.5`, com a chave que já existe) ou pela chave
  da Anthropic (`claude-haiku-5-5`). Entrar com a conta Claude (o login OAuth de uma
  assinatura, usado pelo Claude Code) **não** é oferecido: os termos de consumidor da Anthropic
  não cobrem esse uso fora do Claude Code e do Claude.ai desde 02/2026. A chave de API tem
  limite de gasto próprio no Console da Anthropic.
- **Chave de outro provedor.** O servidor guarda a chave no provedor que está sendo
  escolhido. Por isso a chave de um provedor que não está em uso entra junto com a troca para
  ele: **Definir nova chave…** leva para **Trocar**, com o provedor já escolhido. Nada troca
  sem confirmação.
- **Preços.** Vêm do servidor, que consulta a lista pública do OpenRouter no máximo uma vez a
  cada 24 h e usa uma tabela própria quando está sem rede. O Dotdeck mostra "aproximado" e não
  inventa: sem preço, a linha some.
- **Voz.** A página Chamadas de voz traz o grupo do modelo logo abaixo do status, com as três
  linhas e a troca do modelo que fala (GPT Audio Mini ou GPT Audio). A ponte de voz passa a
  informar o modelo de voz e o de transcrição em `GET /health`. O log de cada chamada diz qual
  modelo atendeu.
- **Modelo à vista.** O rodapé da barra lateral mostra "Claude Haiku 5.5 · OpenRouter" e abre
  Modelos de IA.
- **Servidores antigos.** Um servidor configurado com o Dotdeck 0.3.2 não informa provedores,
  preços nem a transcrição. A página mostra só o que ele informa, sem o menu de provedor e sem
  o teste.
- **Tradução.** "Nenhuma chave ainda" ganhou uma mensagem própria. pt_BR e es seguem 100%
  traduzidos, e um teste confere isso.

**Pedidos do dono:** "Quando alguém baixar [o Dotdeck da loja], ele vai zerado sem os meus
dots e chaves de api, certo?" e "Quero poder adicionar provedores como Claude além da chave de
API ... usar o novo modelo Haiku ... Também preciso deixar mais claro qual modelo ele está
usando para chamadas e como configurar".
