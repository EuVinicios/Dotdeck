# Changelog

Todas as mudanças relevantes do Dotdeck. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o
[SemVer](https://semver.org/lang/pt-BR/).

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
