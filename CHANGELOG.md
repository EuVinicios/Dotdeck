# Changelog

Todas as mudanças relevantes do Dotdeck. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o
[SemVer](https://semver.org/lang/pt-BR/).

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
