# Changelog

Todas as mudanças relevantes do Dotdeck. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o
[SemVer](https://semver.org/lang/pt-BR/).

## [0.3.8] - 2026-10-12

### Corrigido

- **A caixa de mensagem cresce com o texto**, linha a linha, como nos apps de chat: na conversa
  e no começo de uma conversa nova. Antes ficava presa numa linha (até 120 px, com rolagem
  interna) e, numa conversa nova, em duas. Ela para em 40% da altura da janela (no máximo
  320 px) e rola a partir daí; volta a uma linha quando a mensagem é enviada, e a conversa
  acima continua no fim enquanto se digita.

## [0.3.7] - 2026-10-10

### Corrigido

- **Menu do Dot (botão direito)**: abria 13 px mais baixo que o próprio conteúdo, e o último
  item ("Tornar Chefe", ou "Ocultar da barra lateral" no Chefe) ficava cortado atrás de uma
  rolagem, em qualquer Dot e em qualquer tamanho de janela, no X11 e no Wayland. O GTK media o
  menu antes de a linha entre as duas seções aparecer e, preso à lista de Dots, nunca o media
  de novo. Agora o menu pertence à barra lateral, que o reajusta (ADR-020).
- O primeiro item do menu do Dot dizia "Aberto"; agora diz **"Abrir"**. O selo do servidor
  aberto em Configurações › Servidores passa a dizer "Atual".
- **F5 ou um token novo durante uma chamada** encerram a chamada (e o toque, se ainda estava
  chamando). Antes, a página nova não sabia da chamada antiga, e o microfone e o alto-falante
  continuavam ligados sem página.
- **Configurações** abre com 760 px de altura (menos numa tela baixa): a lista de seções cabe
  inteira, sem "Avançado" cortado nem "Sobre" escondido, e Privacidade não rola mais. Numa
  janela estreita, "Editar o Dot…", "Microfone…" e os outros atalhos abrem a página pedida, e
  não a lista, e cada página ocupa só a largura de que precisa: antes todas ficavam tão largas
  quanto Aparência e passavam da borda direita.
- **Galeria de pets**: os 12 pets cabem sem uma rolagem de 7 px.
- **Sem conexão com o servidor**: o botão Ligar fica apagado, com o motivo na dica, e a barra
  de erro da página em inglês ("Load failed") some enquanto o aviso do Dotdeck está na tela.
- "Salvar conversa como página…" e "Agendar uma tarefa…" ficam apagados enquanto o Dot não
  tem conversa (antes só davam um aviso de erro).
- Servidor sem Dots: o cabeçalho diz "Nenhum Dot ainda", e não "0 Dot", e usa o nome que você
  deu ao servidor, o mesmo do seletor.

### Adicionado

- **Menu do Dot pelo teclado**: Shift+F10 ou a tecla Menu abrem o menu do Dot em foco, como nos
  campos de texto do GNOME. O Dot fica destacado enquanto o menu dele está aberto, e o foco
  volta para ele quando o menu fecha (também depois de Fixar no topo).

### Alterado

- **Cabeçalho da chamada**: "Chamando…" até a ligação conectar e depois "Em chamada · 01:23",
  com algarismos de largura igual. Se o Dot ouve, pensa ou fala, quem mostra é o pet (e a dica),
  e o título não pula mais de lugar a cada fase. O botão vermelho diz só "Encerrar".
- **Ao clicar em Ligar**, o botão mostra "Chamando…" com um indicador girando e não aceita um
  segundo clique até a chamada conectar (ou por até 25 s).
- **Janela estreita** (até 480 px): "Conversas" vai para o menu principal e o nome do Dot cabe
  no cabeçalho; durante a chamada o subtítulo mostra só o tempo.
- O menu do Dot abre à direita do ponteiro, sem cobrir o nome e a função do Dot.
- Segurar o botão do mouse sobre um Dot não abre mais o menu; o toque longo vale só para telas
  de toque.
- "Atividade" ganhou um ícone próprio; era o mesmo relógio de "Conversas".
- Modelos de IA: os provedores que você não usa aparecem sem o triângulo de aviso.
- Barra lateral: "Dots ocultos" e "Espaços" alinhados com o título "Dots".
- Textos mais claros: "Barra lateral do Dotdeck" (era "Barra lateral nativa"), "Pronto neste
  computador" no motor de voz, um exemplo de endereço só, sem quebra no meio, na tela de
  conexão, "Começar de um exemplo" no Novo Dot e "Ligar ou encerrar a chamada" nos atalhos.

## [0.3.6] - 2026-10-10

### Corrigido

- **Ligações (gateway de voz)**: com um fone de verdade, o Dot não ouvia nada. O detector de
  fala aprendia o ruído de fundo só entre uma frase e outra, partindo de −70 dB; o chiado de
  um fone com fio e ganho automático (por volta de −40 dB) virava "fala" desde o primeiro
  instante, e o fim da frase nunca chegava. Agora o ruído de fundo é medido o tempo todo (o
  décimo mais baixo dos últimos 5 s, até −30 dB) e uma frase tem no máximo 15 s. Os
  primeiros 800 ms da ligação não contam como fala, para o aviso de conexão não cortar a
  saudação do Dot quando o som sai pela caixa.
- **Servidor**: o gateway de voz e o atualizador voltam sozinhos se forem encerrados
  (`Restart=always`); antes, um encerramento da sessão os deixava desligados.

### Adicionado

- **O Dot atende a ligação**: ao conectar, ele diz uma frase curta (o nome e como pode
  ajudar), a prova de que a chamada está no ar. `VOICE_GATEWAY_GREETING=0` desliga.
- **Sons da chamada**, como num app de ligação: enquanto a chamada conecta, o Dotdeck toca
  (dois toques suaves e uma pausa, por no máximo 45 s); quando ela fica no ar, um aviso curto
  de duas notas que sobem; e ao desligar (por você, pelo Dot ou pelo servidor), duas notas
  que descem, mais graves se a chamada caiu com erro. Os sons são gerados no próprio código,
  sem arquivos de áudio, tocam no alto-falante escolhido para as chamadas e nunca entram na
  ligação; o aviso de conexão é curto (0,14 s) e baixinho. Liga e desliga em Configurações ›
  Chamadas de voz › Sons da chamada (ligado por padrão); desligado no meio do toque, ele para
  na hora.

### Alterado

- Em português, o modelo "Caretaker" do Novo Dot passa a se chamar **Manutenção**.
- Os exemplos das decisões (ADR-012 e ADR-013) e dos testes usam só os Dots fictícios das
  capturas (ADR-019).

### Removido

- `data/icons/hicolor/1024.tmp.png`, um arquivo de trabalho que tinha ficado no repositório.

## [0.3.5] - 2026-10-10

Site oficial e capturas novas.

### Adicionado

- **Site oficial** em <https://euvinicios.github.io/Dotdeck/>, em português, claro e escuro:
  recursos, Dots e pets, chamadas de voz, modelos de IA e custos, privacidade, perguntas
  frequentes e como instalar. Fica em `website/`, sem nenhum recurso externo (fontes,
  scripts ou rastreadores), e o GitHub Pages publica a cada mudança na `main`
  (`.github/workflows/pages.yml`, com a busca de privacidade no site gerado). `make site` monta
  o site em `build/website`, copiando o ícone, os pets e as capturas do próprio app.
- **Capturas novas** da interface real com Dots fictícios, em inglês (`data/screenshots`, para
  o AppStream) e em português (`data/screenshots/pt_BR`, para o site e a loja), no claro e no
  escuro, incluindo Modelos de IA e Chamadas de voz. O rodapé da barra mostra "Claude Haiku 5.5
  · OpenRouter".
- **Imagens da loja** em português (`data/store`): banner, ícone e cinco slides, um deles de
  Modelos de IA, e o cartão de prévia do site (`social-card.png`, 1200×630).

### Alterado

- O endereço do Dotdeck no AppStream, no snap, no `.deb` e na janela Sobre passa a ser o site.
- `tools/demo_server.py` responde `GET /api/model-config` e informa o modelo em uso, com Dots
  fictícios também em português (`--lang pt`). `make screenshots OPENDOTS_CLIENT=…` gera as
  duas línguas, claro e escuro, com a fonte do GNOME (Adwaita Sans) e microfones fictícios.

### Corrigido

- Textos em português que a fonte padrão do GNOME cortava: a busca das Configurações agora é
  "Pesquisar configurações", e os estados do pet são Livre, Ocupado, Falando e Pausado.

## [0.3.4] - 2026-10-10

### Corrigido

- **Ligações**: ligar para um Dot falhava com "Dotdeck only carries the microphone in calls."
  Depois de uma limpeza de memória do WebKit, que acontece na página real do OpenDots, a página
  ganhava um `navigator.mediaDevices` novo e voltava a usar o microfone nativo, que a ponte de
  voz não leva. A troca do microfone agora fica em `MediaDevices.prototype`, que dura enquanto
  a página existir. Um teste força a limpeza de memória e faz a ligação.

## [0.3.3] - 2026-10-10

### Adicionado

- **Modelos de IA** (antes "Modelo de IA"): o modelo das conversas aparece pelo nome, com o
  id e o provedor embaixo, o **custo aproximado** por milhão de tokens, o estado da chave e o
  botão **Testar agora**, que mostra em quanto tempo o modelo respondeu (ou o erro).
- **Provedor**: OpenRouter, Anthropic com chave de API (Claude Haiku, Sonnet e Opus) ou um
  servidor compatível com OpenAI (endereço em **Avançado**). Cada provedor tem a sua chave, e a
  lista de modelos mostra preço e velocidade de cada um. O Dotdeck nunca mostra nem guarda
  chaves.
- **Modelo das chamadas de voz**: as três partes de uma chamada (quem fala, quem transcreve e
  quem pesquisa, que é o modelo das conversas) aparecem em Modelos de IA e em Chamadas de
  voz, logo abaixo do status. Em Chamadas de voz dá para trocar o modelo que fala (GPT Audio
  Mini ou GPT Audio).
- **O modelo à vista**: o rodapé da barra lateral mostra o modelo em uso (por exemplo "Claude
  Haiku 5.5 · OpenRouter"); um clique abre Modelos de IA.
- **Ponte de voz**: `GET /health` informa o modelo de voz e o de transcrição, e o log de cada
  chamada diz qual modelo atendeu (inclusive quando o OpenDots pede um id que a ponte não usa).
- **Privacidade na CI**: `make privacy` (`tools/privacy_scan.py`) procura chaves, tokens de
  bot, pastas pessoais e endereços privados no código, e o fluxo do snap faz a mesma busca no
  pacote gerado. Os padrões são genéricos, sem nenhum valor real.

### Alterado

- Provedor, preços, teste e modelo de transcrição dependem do servidor OpenDots atualizado
  (`GET /api/model-config` com provedores). Servidores configurados com o Dotdeck 0.3.2
  continuam funcionando: a página mostra só o que eles informam.

### Corrigido

- A chave sem valor aparecia como "Não definido" (masculino); agora é "Nenhuma chave ainda".
- Um exemplo no código citava o nome do dono; virou um exemplo fictício.

## [0.3.2] - 2026-10-10

### Corrigido

- **Snap:** o slot `dbus` da 0.3.1 exige revisão manual da Snap Store. No lugar dele,
  o Dotdeck roda no snap como GApplication não exclusivo e garante uma única
  instância por conta própria: uma trava e um socket na pasta privada do snap. Abrir
  de novo, `--toggle` e `--toggle-mute` repassam o comando ao app aberto
  (`src/core/instance.py`).

## [0.3.1] - 2026-10-10

### Corrigido

- **Snap:** o app parava em "Failed to register" e não abria a janela, porque o
  confinamento não deixava o Dotdeck usar o próprio nome no D-Bus. O snap agora declara
  esse nome (slot `dbus`). O problema vinha desde a 0.1.0 e não aparecia na CI, que só
  rodava `--version` e `--self-test`; a CI agora abre a janela do snap instalado.
- Teste de modo foco que às vezes lia o estilo antes de a página desenhar.

## [0.3.0] - 2026-10-10

### Adicionado

- **Barra lateral nativa** com todos os Dots: pet, nome e função de cada um (tirada da primeira
  linha das instruções), Espaços, e Atividade, Memórias e Configurações no rodapé. Busca
  (`Ctrl+K`), fixar e ocultar Dots, menu de contexto (abrir, nova conversa, ligar, trocar pet,
  editar, tornar Chefe). A página do OpenDots roda em modo foco, sem a barra dela.
- **12 pets de pelúcia** próprios, um por Dot, sem repetir; galeria para trocar, com troca
  automática entre dois Dots. Os mascotes da página passam a ser os mesmos pets.
- **Janela de Configurações** com seções e busca: Geral, Aparência (tema, densidade, animações
  dos pets), Notificações por evento, Atalhos, Dots (editor completo), Voz e chamadas
  (microfone, alto-falante, teste de microfone, eco, ruído, aperte-para-falar), Modelo de IA,
  Servidores, Privacidade, Avançado e Sobre.
- **Chamadas com os Dots:** botão Ligar no topo, no menu do Dot e na bandeja, `Ctrl+Shift+L`,
  tempo da chamada e aviso antes de sair de uma chamada.
- **Novo Dot** a partir de modelos (Chefe de gabinete, Pesquisador, Escritor, Finanças, Casa e
  infraestrutura, Parceiro de estudos).
- Atalhos no estilo Grok Bot: `Ctrl+1…9` abre um Dot, `Alt+↑/↓` troca de Dot, `F9`
  mostra ou esconde a barra.
- Notificação quando o servidor cai e quando volta; conteúdo das notificações pode ser privado.

### Alterado

- O topo mostra o Dot aberto (pet, nome e função) em vez do nome do servidor.
- A ponte de voz registra cada chamada no diário com o nome `opendots-voice-gateway`, mantém a
  ordem "você → Dot" no resumo e não perde a sua fala enquanto o Dot pesquisa.

### Removido

- `tools/update_store_listing.py`, que lia os cookies do navegador para editar a ficha da loja.

## [0.2.0] - 2026-10-09

### Adicionado

- Botão **Atualizar** do servidor: Preferências → Servidores mostra a versão instalada, as
  novidades do OpenDots oficial e os seus commits locais; a atualização mescla, compila,
  reinicia, confere e volta sozinha se algo falhar. Aviso por notificação e faixa na janela.
- `server/updater`: o atualizador que roda ao lado do OpenDots (porta 4311, com o token).
- `server/voice_gateway`: ponte de voz compatível com a API Realtime que usa o
  `openai/gpt-audio-mini` do OpenRouter, com legenda, interrupção e a ferramenta `ask_compute`.
- `server/install.sh`: instala os dois serviços (simulação por padrão, `--apply`, `--uninstall`).
- Status da voz do servidor nas Preferências.

## [0.1.0] - 2026-10-09

Primeira versão pública.

### Adicionado

- Configuração guiada: teste de conexão, escolha automática de `https`/`http` e da porta 4310,
  mensagens claras para token ausente ou inválido, servidor só local e certificado inválido.
- Vários servidores, cada um com cookies e dados separados; troca pela barra de título,
  pela bandeja ou com `Alt+1…9`.
- Token de acesso no chaveiro do sistema, sem fallback em texto puro.
- Chamadas de voz com os Dots por um motor WebRTC nativo (GStreamer), com cancelamento de eco,
  mudo na barra de título e aperte-para-falar.
- Notificações de tarefas concluídas, com falha e agendadas que começaram.
- Ícone na bandeja (StatusNotifierItem), segundo plano e início com a sessão.
- Atalhos globais pelo portal do sistema (`Super+Shift+D`, `Super+Shift+M`).
- Interface em inglês, português do Brasil e espanhol.
- Pacotes snap (Central de Aplicativos do Ubuntu), Flatpak e `.deb`.
