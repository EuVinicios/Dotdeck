# Publicar o Dotdeck na Flathub

A Flathub é o caminho para as outras distribuições (Fedora, Arch, Debian, Mint…). O manifesto
de desenvolvimento está em
[`build-aux/flatpak/io.github.euvinicios.Dotdeck.json`](../build-aux/flatpak/io.github.euvinicios.Dotdeck.json):
runtime GNOME 51, nenhum módulo extra (Python, PyGObject, WebKitGTK 6, libsecret, GStreamer com
WebRTC e o portal Secret vêm do runtime).

## Permissões e por quê

| Permissão | Motivo |
|---|---|
| `--share=network` | falar com o servidor OpenDots |
| `--socket=wayland`, `--socket=fallback-x11`, `--device=dri`, `--share=ipc` | janela e aceleração do WebKit |
| `--socket=pulseaudio` | microfone e alto-falante das chamadas (PipeWire atende pelo protocolo Pulse; não existe portal de microfone) |
| `--talk-name=org.kde.StatusNotifierWatcher` | ícone na bandeja |

Notificações, atalhos globais, segundo plano, chaveiro (portal Secret) e abrir links no
navegador usam portais: nenhuma permissão extra.

## Testar como a Flathub testa

```bash
make flatpak
```

```bash
make flatpak-lint
```

```bash
flatpak run io.github.euvinicios.Dotdeck --self-test
```

## Submeter

1. Crie a tag da versão (ex.: `v0.1.0`) e confira que o workflow de release passou.
2. Faça um fork de <https://github.com/flathub/flathub> e crie a branch a partir de
   `new-pr`.
3. Na raiz do fork, crie `io.github.euvinicios.Dotdeck.json` igual ao manifesto local, trocando
   a fonte `"type": "dir"` por:

   ```json
   {
     "type": "git",
     "url": "https://github.com/EuVinicios/Dotdeck.git",
     "tag": "v0.1.0",
     "commit": "<hash completo do commit da tag>"
   }
   ```

4. Abra o PR contra a branch `new-pr` com o título `Add io.github.euvinicios.Dotdeck`.
5. Um revisor roda o build (comente `bot, build` no PR). Depois de aprovado, a Flathub cria o
   repositório `flathub/io.github.euvinicios.Dotdeck` e convida você.
6. **Verificação:** entre em <https://flathub.org> com o GitHub (conta `EuVinicios`) e
   verifique o app no portal do desenvolvedor. Para IDs `io.github.*` basta o login.

## Pontos de atenção na revisão

- **"Web wrapper":** a Flathub recusa wrappers simples de sites. O Dotdeck tem integração
  real (configuração nativa de servidores, chaveiro, motor de voz nativo, notificações de
  tarefas, bandeja, atalhos globais, segundo plano). Vale citar isso na descrição do PR.
- **Marca:** nome e ícone próprios (ADR-001 e ADR-005). Não use o nome "OpenDots" nem o
  mascote da CopilotKit nas capturas de tela além do que aparece na página do próprio servidor.
- **Capturas de tela:** precisam estar acessíveis nas URLs do metainfo (branch `main`).
