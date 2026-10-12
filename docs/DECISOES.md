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
(`# Manutenção — backups e o servidor de casa`), porque o OpenDots não tem um campo para isso.
A página roda em "modo foco" (`src/web/focus.js`): o script reconhece a página, esconde a barra
e o topo dela e recebe os cliques da barra nativa. Funciona com qualquer servidor OpenDots. Se a
página não for reconhecida (uma versão com outro layout), a barra nativa sai de cena e a página
volta a mostrar a navegação dela, como no 0.2. O modo foco pode ser desligado em Configurações
→ Aparência.

Os dados vêm de `/api/workspace` e `/api/state`, consultados a cada 4 s com a janela aberta e a
cada 20 s em segundo plano. O OpenDots não tem um canal de eventos; a própria página consulta a
cada 3 s.

**Pedido do dono:** "Veja que a barra lateral onde ficam os dots mostra apenas 3, está tudo muito
apertado e nada profissional como é o GrokBot por exemplo."

> Nota (10/10/2026): o exemplo de primeira linha foi anonimizado e usa um dos Dots fictícios
> das capturas (ADR-019).

## ADR-013 — Pets próprios, um por Dot (10/10/2026)

**Contexto.** A página escolhe o mascote de cada Dot entre só 4 imagens, por um hash do id. Nos
8 Dots do dono, três eram o mesmo pet verde (por exemplo, Dot, Manutenção e Estratégia), e o
desenho aparecia com 30 px, sem rosto legível.

**Decisão.** São 12 pets de pelúcia próprios (`src/core/pets.py`), desenhados fio a fio pelo
mesmo processo do ícone (`tools/render_pets.py`, 256 px em `src/pets/`). Cada pet tem uma cor e
uma silhueta diferente (fone, broto, orelhas de raposa, antena, coroa, laço, óculos, urso, gorro,
coelho, capelo, capacete), para serem reconhecíveis mesmo pequenos. Os pets são atribuídos
sozinhos pela função do Dot (dinheiro → coroa, servidor → capacete, pesquisa → capelo), sem
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

> Nota (10/10/2026): os nomes dos Dots nesta ADR foram anonimizados e são os Dots fictícios das
> capturas (ADR-019); o fato registrado (três dos 8 Dots com o mesmo pet) não mudou.

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

**Escolhas do dono (10/10/2026):** modelo das conversas "Haiku 5.5 pelo OpenRouter
(Recomendado)" e publicação "Sim, direto no stable (Recomendado)" — a 0.3.3 segue edge →
candidate → stable depois dos testes, como a 0.3.2.

## ADR-018 — Site oficial no GitHub Pages e capturas novas (10/10/2026)

**Contexto.** Com a 0.3.4 indo para o canal estável, a ficha da loja e o README ainda mostravam
as capturas da 0.3.0: sem a página Modelos de IA, sem o modelo no rodapé da barra e só em
inglês. O endereço do Dotdeck na loja, no AppStream e no `.deb` era o repositório. O PodFlow
já tem um site no GitHub Pages (`website/`, `build-aux/website.sh` e
`.github/workflows/pages.yml`), e o dono pediu o mesmo para o Dotdeck.

**Decisão.**
- **Site.** `website/` com a mesma estrutura do PodFlow e identidade própria: o azul do pet
  de pelúcia, o azul-marinho das imagens da loja e o lema "Seus Dots na área de trabalho".
  Seções: recursos, Dots e pets (os 12 pets), chamadas de voz, modelos de IA e custos,
  privacidade (nada vai para o desenvolvedor, e o app da loja vem vazio), perguntas
  frequentes e instalação em três passos (loja, `snap connect` das duas interfaces e
  conexão ao OpenDots, com o link do projeto). Claro e escuro pelo sistema, a partir de
  360 px de largura, sem fontes, scripts ou rastreadores de fora; o único script é o botão
  Copiar dos comandos. `build-aux/website.sh` copia o ícone, os pets, as capturas em
  português e o cartão de prévia, e o workflow Site publica a cada mudança na `main` depois
  de passar o site gerado pelo `tools/privacy_scan.py`.
- **Endereço.** `https://euvinicios.github.io/Dotdeck/` passa a ser o endereço do Dotdeck no
  metainfo (`homepage`), no `snapcraft.yaml` (`website`), no `.deb` (`Homepage`) e na janela
  Sobre. Código-fonte, relatos e contribuição continuam apontando para o GitHub.
- **Capturas.** Saem da interface real com Dots fictícios (`tools/demo_server.py`), agora com
  `GET /api/model-config` (Claude Haiku 5.5 pelo OpenRouter) e o modelo em uso no
  `/api/workspace`. Os Dots têm nomes genéricos, escolhidos para não lembrar os de ninguém
  (Assistente, Pesquisa, Redação, Finanças, Manutenção, Laboratório, Estratégia, Estudos, e
  os mesmos em inglês), o servidor se chama Aurora e os microfones são fictícios. `make screenshots` gera inglês
  (`data/screenshots`, usado pelo AppStream) e português (`data/screenshots/pt_BR`, usado
  pelo site e pela loja), claro e escuro, com a fonte do GNOME (Adwaita Sans 11) em vez da
  DejaVu Sans de um servidor X sem configuração.
- **Loja.** `make store-assets` gera, em português, o banner, o ícone, cinco slides
  (Dots, pets, Modelos de IA, chamadas de voz no escuro e configuração guiada) e o cartão
  de prévia do site. O envio continua pelo painel do snapcraft.io, na sessão do dono
  (ADR-016).
- **Textos cortados.** Na fonte padrão do GNOME, "Pesquisar nas configurações" e os estados
  "Em repouso" e "Trabalhando" do pet não cabiam. Viraram "Pesquisar configurações",
  "Livre" e "Ocupado".
- **Só promete o que funciona para quem instala.** Modelos de IA (o modelo no rodapé, os
  preços, a troca e o teste) dependem de `GET /api/model-config`, e a voz só com a chave do
  OpenRouter depende da ponte de voz e de o OpenDots aceitar outro endereço de voz
  (`VOICE_REALTIME_URL`). O OpenDots oficial ainda não tem nenhum dos dois, e nele o áudio de
  uma chamada vai do Dotdeck direto para a API Realtime da OpenAI. O site diz isso nas seções
  de modelos, de voz e de privacidade, e a frase de abertura fala de pets, chamadas e avisos.

O site só fica no ar depois do merge e de uma configuração única no GitHub: Settings → Pages
→ Build and deployment → Source: GitHub Actions (como no PodFlow).

**Pedido do dono:** "Depois de promover a versão para estável, temos que fazer o mesmo que
fizemos com o PodFlow e colocar os prints do app no site oficial do linux, criar um site no
git como temos do Podflow e deixar tudo com a cara mais profissional possível".

## ADR-019 — Exemplos anonimizados no repositório público (10/10/2026)

**Contexto.** A revisão do site (PR #9) achou dados do dono no repositório público: as ADR-012
e ADR-013 citavam nomes e funções dos Dots reais do servidor dele, a tradução em português do
modelo "Caretaker" do Novo Dot tinha o nome de um desses Dots, e os testes repetiam o conjunto
inteiro, com a função de cada um. Também havia um arquivo de trabalho esquecido no git,
`data/icons/hicolor/1024.tmp.png`, que nada usa.

**Decisão.**
- **Um só conjunto de exemplos.** Documentos, testes e traduções usam os Dots fictícios das
  capturas (`tools/demo_server.py`): Assistente, Pesquisa, Redação, Finanças, Manutenção,
  Laboratório, Estratégia e Estudos, os mesmos em inglês, e o servidor Aurora. As ADR-012 e
  ADR-013 mantêm o sentido e ganham uma nota de que os exemplos foram anonimizados.
- **O que fica.** O vocabulário de que o app precisa para funcionar: os nomes que
  `chief_dot` reconhece como o Dot chefe (`src/core/monitor.py`, entre eles "Chefe"), as
  palavras que escolhem o pet (`src/core/pets.py`) e "Chefe" como nome do papel na interface
  ("Falar com o Chefe"). São palavras comuns, não dados de alguém, e os testes que conferem
  esse palpite continuam usando "Chefe".
- **Modelo do Novo Dot.** Em português, "Caretaker" passa a ser "Manutenção"; o pet continua o
  capacete, pela palavra "manuten".
- **Sem lista de nomes na busca de privacidade.** `tools/privacy_scan.py` só procura formatos
  genéricos; uma lista com os nomes reais publicaria justamente os nomes. A conferência fica
  na revisão.
- **Arquivo esquecido.** `1024.tmp.png` sai do git e `*.tmp.png` entra no `.gitignore`.
- **Histórico.** Os commits antigos continuam com os nomes. Reescrever o histórico do
  repositório público só se o dono pedir.

## ADR-020 — Menu do Dot que cabe inteiro e o polimento de layout (10/10/2026)

**Contexto.** O menu do botão direito sobre um Dot abria 13 px mais baixo que o conteúdo, e o
último item só aparecia rolando, em todos os Dots, em qualquer tamanho de janela, no X11 e num
GNOME Shell com Wayland (GTK 4.24.0 e Adw 1.10.0, as versões do snap). A causa está no GTK: um
popover mede a janela dele uma vez, ao abrir, e só volta a medir quando o widget pai chama
`gtk_popover_present()` na própria alocação, o que os gerenciadores de layout fazem sozinhos. O
menu era filho do `Gtk.ListBox`, cuja alocação não faz isso, e o `GtkMenuSectionBox` acrescenta a
linha entre as seções num idle, depois da primeira medida (1 px + 2 × 6 px de margem). Uma
revisão da interface achou ainda: o item "Abrir" traduzido como "Aberto" (a mesma mensagem do
selo do servidor aberto), o cabeçalho da chamada pulando de lugar a cada fase e com o tempo em
dobro, nenhum sinal entre o clique em Ligar e a chamada conectar, Ligar ativo sem conexão,
Configurações cortada na altura padrão e uma chamada que continuava sem página depois de F5.

**Decisão.**
- **O menu do Dot é filho da barra lateral** (um `Adw.Bin`, que reapresenta os popovers a cada
  alocação), com o ponto do clique convertido para as coordenadas dela. Não vai na linha porque
  Fixar e Ocultar refazem as linhas com o menu aberto. Abre à direita do ponteiro
  (`halign=START`, como os menus de contexto do próprio GTK), também com Shift+F10 e a tecla
  Menu, e o toque longo vale só para telas de toque. Como a barra lateral não recebe foco, o
  GTK deixava o foco cair fora da lista quando o menu fechava; ele volta para o Dot (ou para a
  lista, se o Dot saiu dela), e a lista refeita por Fixar mantém o foco no mesmo Dot. Um teste
  abre o menu em cada Dot e confere que a rolagem interna não tem nada a rolar; ele falha com o
  código antigo (281 > 268) e passa no SDK do GNOME 51 e no runtime do próprio snap.
- **Uma mensagem por sentido**: o menu diz "Abrir"; o selo do servidor usa "Current" ("Atual").
- **Cabeçalho da chamada de largura fixa**: "Chamando…" até conectar, depois "Em chamada ·
  01:23" com algarismos tabulares; a fase fica no pet e na dica. Do clique em Ligar até a página
  conectar (ou 25 s), o botão mostra um indicador e "Chamando…" e não aceita outro clique.
- **Uma página nova encerra as chamadas da antiga**: no `load-changed` COMMITTED, se houver
  chamada, `close_all()`.
- **Janelas estreitas**: abaixo de 480 sp, "Conversas" vai para o menu principal.
- Configurações abre com 980 × 760 (limitado à altura da tela), e a galeria de pets com 712 px.
  As páginas de Configurações deixam de ter a mesma largura (`hhomogeneous=False`): a 360 px só
  Aparência ainda passa da borda, por causa dos seletores lado a lado.

**Fora desta rodada** (baixo impacto ou risco de regressão): os dois botões "+" vizinhos, um
botão de criar o primeiro Dot na lista vazia, trocar os IDs de modelo por nomes, o texto de
telemetria em Privacidade e as mensagens de erro do servidor em inglês nos avisos.

**Pedido do dono:** "Outro ponto, ao clicar com o botão direito sobre os dots aparece um menu
de interação, mas tenho que rolar para ver todas as opções. Faz um polimento final desses
detalhes de layout e UX."

## ADR-021 — Novidades do OpenDots oficial: testes antes de reiniciar, prévia de conflitos e contrato diário (12/10/2026)

**Contexto.** O Dotdeck publicado depende do OpenDots oficial em dois pontos.
- **A página.** O modo foco e os comandos da barra nativa acham os elementos da página pelas
  classes, pelos rótulos acessíveis em inglês e por algumas palavras visíveis (ADR-012). Se o
  oficial muda esses nomes, o modo foco se desliga sozinho e a página volta a ser mostrada
  como é, mas os comandos da barra nativa (abrir um Dot, Atividade, ligar…) param. Um PR
  aberto no projeto oficial (CopilotKit/OpenDots#121, interface em pt-BR, en, es e fr) troca
  vários desses rótulos ("Dots" vira "Agents", "Open activity" vira "Activity", "Edit X
  settings" vira "Edit settings for X"…) e passa a escolher o idioma pelo navegador. Os
  testes do Dotdeck só rodavam contra uma imitação da página (`tests/fake_server.py`); nada
  conferia o OpenDots de verdade.
- **O servidor.** O atualizador (ADR-009) só fazia `git fetch` a cada 6 horas. Ao clicar em
  Atualizar, mesclava, compilava e reiniciava sem rodar teste nenhum, só descobria um
  conflito tentando o merge, enviava `HEAD:main` ao backup sem olhar a branch, não via
  arquivos fora do git que o merge sobrescreveria (o git sobrescreve os ignorados sem avisar)
  e escutava em `0.0.0.0:4311`, porque herdava o `HOST` do `.env` do OpenDots.

**Pergunta ao dono:** "Atualizações do OpenDots oficial: como proteger o Dotdeck publicado e
o nosso servidor?" **Resposta:** "Alerta + teste contra o oficial (Recomendado)": um aviso
diário quando o oficial tiver novidade, já dizendo se vai dar conflito; um teste diário do
Dotdeck contra o OpenDots oficial; e o atualizador rodando os testes antes de reiniciar e
deixando de escutar em todas as redes. O aviso diário fica fora deste repositório e usa o
`GET /status` descrito abaixo.

**Decisão.**
- **Testes antes de reiniciar.** Depois do merge (e do `npm ci`, se as dependências mudaram)
  o atualizador roda os testes do próprio OpenDots no código mesclado: `npm run typecheck` e
  `npm test`, os que o `package.json` tiver (`--checks`/`UPDATER_CHECKS` escolhem outros
  comandos, `none` desliga). Se falham, o merge é desfeito (`reset` ao ponto anterior e
  `npm ci` de volta, se preciso) e a atualização termina como falha, com as últimas linhas da
  saída. Os testes rodam **antes** do `npm run build`, e não depois: o build reescreve o
  `dist/`, de onde o OpenDots em execução serve a página; assim, um teste vermelho não
  recompila nem reinicia nada e o servidor em uso nem percebe. Build, reinício, checagem de
  saúde e volta ao ponto anterior seguem como na ADR-009.
- **Prévia sem tocar no checkout.** O `GET /status` passa a dizer, a cada consulta (e depois
  de cada `fetch` automático), o que um merge faria agora: `conflicts` (arquivos que vão dar
  conflito com os commits locais, por `git merge-tree --write-tree --name-only`, do git 2.38;
  `null` num git mais antigo), `untracked` (arquivos que o oficial acrescenta e que já existem
  no servidor fora do git, ignorados ou não), `branch` e `expected_branch`, além do `behind`
  que já existia. O formato vai para a versão 2; os campos antigos não mudam.
- **Recusas que não mudam nada.** O atualizador não começa se o checkout não estiver na
  branch `main` (`--branch` muda), num HEAD destacado, com mudanças não salvas ou com arquivos
  fora do git no caminho, e confere de novo depois do `fetch`. O backup recebe
  `refs/heads/main`, e não mais o `HEAD`.
- **Só 127.0.0.1.** O endereço vem de `--host`, senão de `UPDATER_HOST` (no ambiente ou no
  `.env` do OpenDots), senão `127.0.0.1`; o `HOST` do OpenDots não conta mais. O
  `server/install.sh` grava `--host` no serviço; `--updater-host` expõe o atualizador de
  propósito, com um aviso. O `--update` passa a renovar também o atualizador já instalado
  (código e serviço, com cópia dos anteriores, e nunca no meio de uma atualização), e a
  simulação mostra a troca: "escuta em 127.0.0.1:4311 (hoje: 0.0.0.0:4311)". Um Dotdeck em
  outro aparelho chega ao atualizador por um túnel SSH ou por um endereço exposto de
  propósito, informado em **Endereço do atualizador** (`server/README.md`).
- **Na interface.** Em Preferências → Servidores, **Atualização bloqueada** diz o porquê
  (conflito em quais arquivos, arquivos fora do git, outra branch) e o botão Atualizar fica
  apagado; o progresso mostra "Rodando os testes do OpenDots…"; a dica da última atualização
  traz a saída do teste que falhou. Num servidor de outro aparelho sem atualizador à vista, o
  texto explica o túnel e a exposição.
- **Contrato.** `src/web/contract.json` lista os 104 itens que o Dotdeck usa do OpenDots:
  classes, ids, rótulos acessíveis, atributos, palavras visíveis, chaves de armazenamento,
  formatos de link, props do React lidas pelas fibras, o `__reactFiber$` do cliente
  compilado, as rotas da API, as perguntas da página (`window.confirm`/`prompt`) e uma
  suposição: o cliente não ter `opendots-language` (a escolha de idioma do PR de tradução).
  Cinco são opcionais, porque só existem em alguns servidores (as rotas de `/api/model-config`
  do guia do Dotdeck e a lista de Dots recolhível de alguns forks). `tests/test_contract.py`
  confere a lista nos dois sentidos contra `focus.js`, `focus.css`, `webview.py` e o cliente
  da API: uma string nova nesses arquivos precisa ser classificada (da página → contrato; do
  Dotdeck → `not_upstream`), e um item que o Dotdeck deixou de usar sai da lista. Com
  `OPENDOTS_SRC`, o mesmo teste procura cada item num checkout do OpenDots;
  `tools/upstream_contract.py` faz isso na linha de comando e escreve o resumo.
- **Teste diário.** `.github/workflows/upstream.yml` roda todo dia às 06:17 UTC, à mão e nos
  PRs que mexem em `src/web/**`, `src/core/api.py` ou `server/**`: clona o `main` oficial,
  usa o Node que o `engines` dele pede, roda `npm ci && npm run build`, o contrato e o teste
  de fumaça da interface no cliente oficial compilado (`make smoke OPENDOTS_CLIENT=…`, no
  mesmo contêiner e com o mesmo xvfb do CI). Se falhar, o resumo do job lista os itens que
  sumiram e o que cada um quebra.

**Prova.**
- `tests/test_updater.py`: 15 testes (8 novos), com repositórios git temporários e `npm` e
  `systemctl` falsos: teste vermelho desfaz o merge sem build nem reinício e mostra a saída
  sem cores; dependências voltam quando o teste falha depois de um `npm ci`; comandos
  escolhidos e desligados; prévia de conflito sem `MERGE_HEAD` nem mudança no checkout;
  arquivos fora do git, inclusive ignorados, recusados; outra branch e HEAD destacado
  recusados e só a `main` enviada ao backup; endereço de escuta.
- `tests/test_gateway.py`: 4 testes novos do instalador (simulação com a troca de endereço,
  `--host 127.0.0.1` no serviço, `--update` que renova o atualizador mantendo o `--repo` e o
  `--updater-host` escolhido, nada reiniciado durante uma atualização).
- `tests/test_updates_ui.py`: 4 testes novos (bloqueio com arquivos e branch, saída do teste
  na dica, leitura do `/status` antigo e novo, servidor local ou remoto).
- `tests/test_contract.py`: 6 testes. Contra o `main` oficial (`625452e`, 06/10/2026), os 99
  itens obrigatórios estão lá. Contra o PR #121 (`664f86d`), 14 quebram, entre eles `Dots`,
  `Open activity`, `Edit ${name} settings`, `New chat` e o `opendots-language` que apareceu.
- Teste de fumaça no cliente compilado (contêiner do CI, 4 GB): passa no `main` oficial e
  falha no PR #121 ("the page could not open Study"), que a versão anterior do teste de
  fumaça deixava passar, porque só conferia o modo foco e as Configurações. Com a imitação da
  página (`make smoke`, o caminho do CI) também passa.
- Suíte inteira, um módulo por vez no contêiner do CI, antes e depois: 379 → 401 testes e as
  mesmas 4 falhas nos dois (chamadas WebRTC em loopback num contêiner sem rede).
- No `main` oficial, `npm run typecheck` leva 6,5 s e `npm test` (302 testes) 12 s nesta
  máquina, com Node 22. Numa cópia do OpenDots deste servidor, com os commits locais, os
  dois passam (463 testes em 13 s): os testes padrão não travariam a próxima atualização.

**Limites.** O contrato é textual: mostra que os nomes estão no código, não que a página
funciona. O teste de fumaça cobre o caminho principal (reconhecer a página, ligar o modo foco,
abrir dois Dots, Atividade e Memórias pelos comandos da barra nativa, com a página
confirmando cada um, e abrir as Configurações), sobre a API fictícia do
`tools/demo_server.py`; mudanças na API real só aparecem pelas rotas do contrato. O GitHub desliga workflows
agendados de repositórios públicos depois de 60 dias sem atividade. E os testes do OpenDots
rodam no código do servidor, com os commits locais: se um commit local quebrar um teste, o
atualizador recusa toda atualização até que ele seja corrigido (ou `UPDATER_CHECKS=none`).

**Emenda de 12/10/2026 — o teste diário saiu do GitHub Actions.** O mantenedor pediu que tudo o
que puder rode na máquina dele, em horário que não atrapalhe o servidor. O agendamento diário do
workflow "OpenDots oficial" saiu. No GitHub ele roda só nos pull requests que mexem no código do
contrato, e à mão (`workflow_dispatch`). O teste diário roda de madrugada no servidor do mantenedor,
com as mesmas duas checagens: `tools/upstream_contract.py --opendots <checkout>` e
`make smoke OPENDOTS_CLIENT=<dist/client>` na imagem de CI. Ele roda contra o `main` oficial e
contra o OpenDots que o servidor usa, e só avisa quando o resultado muda. Quem não tem esse
servidor pode rodar o mesmo pelo `workflow_dispatch`.

## ADR-022 — Lançamento automático no merge (12/10/2026)

**Contexto.** Uma mudança chegava ao público em quatro passos manuais. Primeiro, um PR de
versão mudava `src/config.py`, `po/dotdeck.pot`, o CHANGELOG e a metainfo. Depois vinham o merge,
a tag `v*` (que publicava o snap em `candidate`) e a promoção para `stable` no snapcraft.io
(ADR-016). Entre o merge de uma correção e a loja havia sempre um passo esquecível: a 0.3.8 (a
caixa de mensagem que cresce) ficou no `edge` até alguém lembrar de lançar.

**Pergunta ao dono:** "Como você quer que as mudanças cheguem à loja (canal stable da Snap
Store) daqui em diante?" **Resposta:** "Automático no merge (Recomendado)".

**Decisão.**
- **O CHANGELOG decide.** Um PR que acrescenta uma linha em `## [Não lançado]` lança uma versão
  ao ser integrado. Um PR sem linha ali (CI, docs, testes, refatoração) não lança nada.
- **Só depois do CI verde.** O workflow `Lançamento` (`auto-release.yml`) roda no fim do CI de
  cada push na `main` e só segue se ele passou e se a `main` ainda está naquele commit.
  `tools/release.py` escolhe a versão: sobe o patch, ou o minor quando há `### Adicionado`.
  O script escreve a versão nos quatro arquivos e gera o `<release>` da metainfo a partir dos
  comentários `<!-- loja: … -->` (em inglês, o idioma da loja). O workflow confere com
  `make validate`, faz o commit `chore(release): X.Y.Z` e envia a tag `vX.Y.Z`, os dois num
  push atômico.
- **Tag vai direto para `stable`.** O snap da tag é publicado em `stable`, e não mais em
  `candidate`. O mesmo job que publica já instala o snap e roda `--version`, `--self-test`
  (motor de voz pronto, sandbox snap), a varredura de privacidade e a checagem de D-Bus. Se
  algo falha, nada é publicado. Para voltar atrás, basta devolver o canal à revisão anterior
  (`snapcraft release dotdeck <revisão> stable`).
- **Token próprio.** O que o `GITHUB_TOKEN` envia não dispara outros workflows, e a tag precisa
  disparar o Release e o Snap. Por isso o envio usa o segredo `RELEASE_TOKEN`: um fine-grained
  token do dono, só deste repositório, com Contents: Read and write. Sem ele, o workflow só
  avisa.
- **Versão feita à mão continua valendo.** Se `src/config.py` tem uma versão sem tag, o workflow
  só cria a tag. O commit de versão do próprio workflow volta a passar pelo CI e termina sem
  fazer nada, porque não há linha em "Não lançado" e a tag já existe.

**Pedido do dono:** "pensa numa forma de deixar o processo automatizado sempre que eu lançar
uma mudança aqui no aplicativo".

## ADR-023 — Conexões e Aprendizado de cada Dot no editor nativo (12/10/2026)

**Contexto.** O editor do Dot (Configurações › Dots, ADR-014) não tinha o Aprendizado
automático nem as Conexões (servidores MCP, do OpenDots oficial #52). A linha "Conexões e
ferramentas…" fechava as Configurações e abria o formulário de Dot da própria página
(`WorkspaceDialog.tsx`): uma coluna de 460 px com nome, instruções, Espaços, permissões,
Aprendizado e Conexões, nessa ordem. O título e o Salvar sumiam na rolagem, e o fim da caixa
de Aprendizado ficava cortado na borda de baixo. Metade dos campos repetia o editor nativo.

**Referências.** O Grok Bot separa os assuntos. Cada bot tem um painel curto, "Bot settings"
(nome, rótulo, descrição, avatar, notificações). Os plugins ficam em "Your plugins", com uma
chave por ferramenta, e as regras de aprovação ficam nas configurações gerais
(docs.x.ai/grok-bot/settings-and-notifications, 06/10/2026). No OpenDots oficial, nenhum PR
aberto mexe nesse formulário.

**Pergunta ao dono:** "Como você quer resolver a janela de capacidades do Dot?" **Resposta:**
"Tudo nativo (Recomendado)". Ao confirmar, ele perguntou: "tudo nativo não quer dizer que
perderei as possibilidades de configuração que hoje existem, certo?". Nada se perde: cada campo
do formulário da página tem lugar no editor nativo, e as rotas da API são as mesmas.

**Decisão.**
- **Conexões** (`src/ui/settings/connections.py`), depois dos Espaços. Cada servidor MCP é uma
  linha expansível com o endereço, "2 de 3 ferramentas ligadas" e "token salvo", e com os
  botões Atualizar e Remover (Remover pede confirmação). Cada ferramenta é uma linha com um
  menu de três opções: **Permitida**, **Pedir antes** ou **Bloqueada**. São as duas caixas
  da página ("ligada" e "Ask first") num controle só; "Somente leitura" abre a descrição, para
  os menus ficarem alinhados. **Adicionar servidor…** abre um diálogo com nome, endereço
  (conferido como o servidor confere: http(s), sem usuário e senha) e token opcional, que vai
  para o servidor e nunca mais é exibido. Como na página, cada mudança vale na hora, fora do
  Salvar do Dot; se o servidor recusa, o menu volta ao valor anterior. Num servidor sem a
  rota, o grupo diz "Ainda não existe neste servidor" e esconde o Adicionar.
- **Aprendizado automático**, depois das Conexões, faz parte do rascunho do Dot: ID do
  contêiner (validado com a regra do servidor; vazio = fora do Aprendizado), "Usar skills
  publicadas" (apagado sem contêiner, porque o servidor recusa) e o link para o guia. Se os
  dois campos não mudaram, o `PUT` devolve os valores exatos do servidor, como já fazia com as
  instruções. Num servidor sem Aprendizado, o grupo não aparece.
- A linha "Conexões e ferramentas…" saiu. O comando `editDot` da página continua no
  `webview.py`/`focus.js` (e no contrato), mas nenhuma tela o usa.
- **Contrato**: cinco rotas novas, obrigatórias porque o OpenDots oficial já as tem:
  `GET`/`POST /api/dots/:id/connections`, `POST /api/connections/:id/refresh`,
  `PATCH /api/connections/:id/tools/:name` e `DELETE /api/connections/:id`.

**Prova.**
- `tests/test_settings.py`: 6 testes novos, sobre o servidor de gravação, que agora tem as
  rotas de Conexões: os três estados de cada ferramenta e os corpos exatos do `PATCH`;
  Atualizar; um `PATCH` recusado volta o menu; adicionar (corpo exato, token limpo do campo,
  endereço inválido barrado) e remover com confirmação; lista vazia e servidor antigo (404);
  Aprendizado validado, salvo e zerado sem contêiner; grupo escondido sem Aprendizado.
- Suíte local (`LANG=C`): 366 testes, só o erro conhecido do GstWebRTC ausente. Contrato
  conferido contra o fork do servidor e contra o `main` oficial.
- Capturas em pt_BR (a 980 px): grupo de Conexões com um servidor aberto, Aprendizado e o
  diálogo de adicionar.

**Pedido do dono:** "precisamos redesenhar e melhorar o design e a UX da janela de
configurações das capacidades dos Dots. Veja na imagem anexa que abre uma janela estreita em
que parte das informações ficam fora dela. Veja como o Grok Bot e o Dots tratam isso e crie
uma experiência agradável e usual para o cliente final."

## ADR-024 — A ponte de voz ouve o fim da fala em sala barulhenta (12/10/2026)

**Contexto.** As ligações com os Dots pararam de responder. Numa ligação de 37 s com o
Guardião, a ponte transcreveu o pedido do dono e terminou com "0 replies". O
`gpt-audio-mini` respondia em 1,4 s quando testado direto, então o problema era a ponte.
O log com `VOICE_GATEWAY_DEBUG=1` mostrou a causa:
- A voz do dono chegava a −5 dB. O ganho automático do Dotdeck fazia o ruído da sala pular de
  −41 a −27 dB a cada 20 ms.
- O detector julgava cada pedaço de 20 ms sozinho. Um pedaço alto zerava os 700 ms de
  silêncio, e a fala só terminava no corte de 15 s.
- Três pedaços altos seguidos (60 ms) contavam como fala nova e cancelavam a resposta antes
  de qualquer som sair.
- Depois de corrigido isso, sobraram dois problemas: um ventilador distante (rajadas de −17
  a −24 dB) e a própria voz do Dot vazando pelo cancelamento de eco (−36 dB) ainda passavam
  por fala.

**Decisão** (`server/voice_gateway/vad.py` e `call.py`).
- **Decidir por 100 ms.** Início, fim e piso de ruído usam a energia média dos últimos 100 ms.
  Um clique ou uma rajada curta não muda mais nada.
- **Fim relativo à voz.** A fala termina 18 dB abaixo do que se ouviu dela, ou abaixo do
  limite do piso, o que vier antes.
- **Só a voz perto do microfone.** A ponte aprende o volume da voz do dono com as falas que
  responde (sobe rápido; nunca desce por causa de uma fala aceita, só 0,1 dB/s com o tempo).
  Uma fala 10 dB abaixo disso termina vazia: TV, ventilador, a voz do Dot.
- **Interromper só com fala de verdade.** O Dot só é interrompido depois de 250 ms de voz
  perto do microfone, e não no primeiro sinal de som.
- **Filtro de 200 Hz.** O áudio de quem liga passa por `audiocheblimit` (passa-altas, 4
  polos) antes do detector e do modelo. O telefone começa em 300 Hz.
- **O Dot avisa antes de pesquisar.** Uma regra é acrescentada às instruções da ligação.
  Duas `ask_compute` na mesma resposta viram uma com os dois pedidos: o OpenDots roda uma por
  conversa, e a segunda falhava com "Thread is already locked", o que levava o Dot a pedir
  acessos que já tinha.
- **Transcrição sem palavras é descartada** ("d", "."), e o modelo de transcrição é instruído
  a não inventar nada para ruído.

**Prova.** 16 testes novos em `tests/test_gateway.py`, rodados no runtime Flatpak
(`org.gnome.Platform//51`, onde a ponte roda):
- ruído oscilante que nunca é fala;
- fim da fala sobre esse ruído;
- pausas entre palavras de quem fala baixo;
- rajadas que não arrastam a voz aprendida;
- voz distante que não interrompe;
- filtro de 200 Hz que corta 70 Hz em mais de 20 dB e deixa a voz passar;
- pedidos de pesquisa juntados.

Com o código anterior, 5 dos 6 primeiros falham. Nas ligações reais de 12/10, a última
durou 2 min 37 s, com 8 respostas, 4 pesquisas e todas as rajadas do ventilador
ignoradas.

**Pedido do dono:** "estou testando a funcionalidade de ligações e os bots não respondem" e
"o ruído do ventilador que está longe de mim parece que atrapalha ele e fica cortando a
ligação".

## ADR-025 — Uma voz para cada Dot e custos à vista (12/10/2026)

**Contexto.**
- Todos os Dots falavam com a mesma voz (`VOICE_NAME` do servidor).
- O Dotdeck não mostrava quanto uma conversa ou uma ligação custava, nem o gasto da chave do
  OpenRouter. Isso ficava só no painel do OpenRouter.
- O opencode mostra o custo da sessão junto da conversa.
- O runtime do OpenDots guarda os tokens de cada turno, mas descarta o `usage.cost` do
  OpenRouter (o conversor do CopilotKit só mantém contagens de tokens).

**Decisão.**
- **Servidor (fork do OpenDots, `src/server/fork/`).** As tabelas são do próprio fork, numa
  conexão separada do mesmo arquivo SQLite. Nenhuma tabela ou schema do upstream muda.
  - `GET /api/dot-voices` e `PUT /api/dots/:id/voice`: a voz de cada Dot. A sessão da ligação
    usa essa voz, ou o `VOICE_NAME` quando o Dot não tem uma. As vozes `fable`, `nova` e
    `onyx` só aparecem com a ponte de voz.
  - Cada turno é somado à conversa com o custo aproximado: tokens × preço do modelo
    (`model-prices.ts`). O cache de entrada conta como entrada normal.
  - `GET /api/costs/threads/:id` informa esse custo. `POST /api/costs/calls/:id` acrescenta a
    voz de uma ligação: um valor por ligação, que substitui o anterior.
  - `GET /api/spend` traz `/key` (esta chave: dia, semana, mês, total e limite) e `/credits`
    (o saldo da conta) do OpenRouter, guardados por 1 minuto. Um erro nunca fica guardado, e
    a chave nunca sai na resposta.
- **Ponte de voz.** Depois de cada resposta e transcrição, envia `gateway.cost` com o total
  da ligação. Não é um evento da Realtime, e a página do OpenDots o ignora.
- **Dotdeck.**
  - Editor do Dot: grupo **Voz nas ligações**, que salva na hora, como as Conexões.
  - Barra lateral: o gasto do mês e o que resta, abaixo do modelo, com o detalhe na dica.
    Atualiza a cada minuto com a janela aberta e a cada 10 min fora dela. Só aparece em
    servidores com o modelo à vista (fork).
  - Cabeçalho: o custo da conversa aberta. A página passa a informar a conversa aberta
    (`focus.js`), o que dispara o sinal `page-thread`.
  - Ligação: o custo é a voz da ponte mais o que a conversa gastou desde o início da
    ligação (as pesquisas). Ao desligar, a voz é enviada ao servidor.
  - Tudo é opcional no contrato: num servidor upstream nada disso aparece.
- **Vozes iniciais dos Dots do dono.** São dados aplicados pela rota, não código:
  - Maestro: `cedar` (natural e firme)
  - Guardião: `onyx` (grave e firme)
  - Cofre: `ash` (clara e confiante)
  - Faísca: `nova` (viva e animada)
  - Forja: `coral` (calorosa e simpática)
  - Farol: `echo` (calma e ressonante)
  - Tese: `sage` (calma e ponderada)
  - Dot: a do servidor (`marin`)

  As 13 vozes foram conferidas no `gpt-audio-mini` em português, a cerca de US$ 0,0002 cada.

**Prova.**
- OpenDots (fork): 8 testes novos (`tests/fork-costs.test.ts`), suíte de 486 testes,
  `typecheck` e `lint` limpos.
- Dotdeck:
  - textos de custo (`tests/test_costs.py`);
  - monitor que só pergunta a servidores fork e uma vez por minuto;
  - linha da barra lateral;
  - página que informa a conversa aberta (`test_focus`);
  - editor da voz, com o `PUT` exato e um servidor antigo sem o grupo;
  - evento `gateway.cost` que só cresce;
  - captura com o servidor de teste: "US$ 0.29 this month · US$ 6.11 left" sob o modelo e
    "≈ US$ 0.07 in this conversation" no cabeçalho.
- Suíte local (`LANG=C`): só o erro conhecido do GstWebRTC ausente.

**Pedido do dono:** "veja se consegue deixar uma voz diferente para cada bot e vozes que
combinem com eles. Outro ponto que seria legal é ter um contador de custos tanto nas chamadas
quanto nas interações por chat como o opencode já faz e também ter facilmente à vista quanto
já gastei da minha api do openrouter. Esse gasto geral pode ficar no menu à esquerda logo
abaixo do modelo que estamos usando, enquanto chats e ligações no contexto do que está sendo
executado".
