# Publicar o Dotdeck na Central de Aplicativos do Ubuntu

A [Central de Aplicativos do Ubuntu](https://github.com/ubuntu/app-center) mostra os apps da
**Snap Store**. Um `.deb` aberto pelo arquivo aparece sempre como "Potencialmente inseguro",
sem ícone e com "Editor desconhecido": por isso o caminho oficial é publicar o snap.

O empacotamento está pronto em [`snap/snapcraft.yaml`](../snap/snapcraft.yaml): base `core26`
(Ubuntu 26.04) com a extensão `gnome`, que traz GTK 4, Libadwaita, WebKitGTK 6.0, PyGObject e
GStreamer do runtime compartilhado `gnome-core26`. O snap embute apenas os plugins de WebRTC
das chamadas de voz. O workflow [`.github/workflows/snap.yml`](../.github/workflows/snap.yml)
gera o snap para amd64 e arm64 a cada push, instala, roda `dotdeck --self-test` dentro do
confinamento e, com a credencial configurada, publica sozinho.

## 1. Conta de desenvolvedor e nome (uma vez)

1. Entre em <https://snapcraft.io/account> com a mesma conta Ubuntu One do PodFlow.
2. Registre o nome:

   ```bash
   snapcraft login
   ```

   ```bash
   snapcraft register dotdeck
   ```

## 2. Ligar o GitHub à loja (uma vez)

Gere uma credencial válida só para este snap:

```bash
snapcraft export-login --snaps=dotdeck --acls package_access,package_push,package_update,package_release --expires "2027-10-01" credenciais.txt
```

No GitHub, em **Settings → Secrets and variables → Actions → New repository secret**, crie
`SNAPCRAFT_STORE_CREDENTIALS` e cole o conteúdo de `credenciais.txt`. Depois apague o arquivo.

| Evento | Canal da loja |
|---|---|
| push na `main` | `edge` (testes) |
| tag `v*` (ex.: `v0.1.0`) | `stable` (o público) |

As tags saem sozinhas do workflow [Lançamento](../.github/workflows/auto-release.yml) (veja
abaixo). O snap da tag só é publicado depois de instalar e passar no `--self-test`, nas duas
arquiteturas.

## 2b. Lançamento automático (uma vez)

Todo PR integrado à `main` que acrescenta uma linha em `## [Não lançado]` no `CHANGELOG.md`
vira versão sozinho. Quando o CI passa naquele commit, o workflow **Lançamento** roda
`tools/release.py`, que sobe a versão em `src/config.py`, `po/dotdeck.pot`, `CHANGELOG.md` e na
metainfo. Depois faz o commit `chore(release): X.Y.Z` na `main` e envia a tag `vX.Y.Z`. A tag
dispara o **Release** (a release no GitHub, com o `.deb` e o Flatpak) e o **Snap** (canal
`stable`). Um PR sem linha em "Não lançado" (CI, documentação, testes) não lança nada.

- **Número da versão.** Sobe o patch (`0.3.8` → `0.3.9`). Se a seção tem `### Adicionado`,
  sobe o minor (`0.3.8` → `0.4.0`).
- **Texto da loja, em inglês.** Vem dos comentários `<!-- loja: … -->` da seção, um por
  linha: o primeiro é o resumo, os outros viram a lista. Sem nenhum, o resumo fica "Fixes and
  improvements.". Os comentários saem do changelog no lançamento.
- **Versão preparada à mão.** Se `src/config.py` tem uma versão que ainda não tem tag, o
  workflow só cria a tag.

Para o workflow poder enviar a tag, crie um token. O `GITHUB_TOKEN` do Actions não serve: o que
ele envia não dispara outros workflows.

1. Em <https://github.com/settings/personal-access-tokens/new>, crie um **fine-grained token**
   com acesso **só ao repositório Dotdeck**, permissão **Contents: Read and write** e validade
   de até um ano.
2. Em **Settings → Secrets and variables → Actions → New repository secret** do Dotdeck, crie
   `RELEASE_TOKEN` com o token.

Sem o segredo, o workflow só avisa e não lança. Para conferir o que ele faria:

```bash
python3 tools/release.py --dry-run
```

Para tirar uma versão do ar, volte o canal para a revisão anterior em
<https://snapcraft.io/dotdeck/releases> ou com `snapcraft release dotdeck <revisão> stable`.

> **Snapcraft 9.2:** a extensão `gnome` com `core26` chega ao canal estável na versão 9.2.
> Até lá o workflow usa `latest/edge` e aplica a correção da extensão sozinho (o mesmo
> remendo do PodFlow). Quando a 9.2 estiver em `latest/stable`, ele se desliga.

## 3. Pedir conexões automáticas (importante)

Duas interfaces não são conectadas sozinhas e o Dotdeck depende delas:

| Interface | Para quê | Sem ela |
|---|---|---|
| `audio-record` | microfone das chamadas de voz | a chamada avisa: `snap connect dotdeck:audio-record` |
| `password-manager-service` | guardar o token no chaveiro | o token vale só até fechar o app |

Abra um pedido em <https://forum.snapcraft.io/c/store-requests/19> → **New Topic**:

- Título: `Auto-connect audio-record and password-manager-service for dotdeck`
- Texto sugerido: "Dotdeck is a GNOME desktop client for self-hosted OpenDots servers (AI
  assistants). It places voice calls with the user's assistants (audio-record, only while a
  call is open and after the user allows the microphone in the app) and stores the server's
  owner access token in the user's keyring via libsecret (password-manager-service)."

Enquanto isso, o usuário conecta à mão:

```bash
sudo snap connect dotdeck:audio-record
```

```bash
sudo snap connect dotdeck:password-manager-service
```

A interface `unity7` (ícone na bandeja) é conectada automaticamente.

O snap também declara um slot `dbus` com o nome `io.github.euvinicios.Dotdeck`: sem ele, o
confinamento impede o app de registrar o próprio nome no D-Bus e ele para em "Failed to
register" antes de abrir a janela (corrigido na 0.3.1). Dentro do snap, o libsecret guarda o
token num chaveiro próprio e criptografado (`~/snap/dotdeck/current/.local/share/keyrings`),
com a chave entregue pelo portal Secret.

## 4. Página da loja

Em <https://snapcraft.io/dotdeck/listing>:

- **Capturas de tela e banner:** os de `data/store/`, gerados com `make store-assets`
  (banner 1800×600 e 5 capturas 1760×1100 em português, emolduradas com gradiente, título e
  subtítulo). Envie as capturas na ordem do nome do arquivo (`screenshot-1-dots.png` primeiro).
- **Categorias:** "Produtividade" (principal) e "Social".
- **Ícone, título, resumo, descrição, licença e links** já vêm do `snapcraft.yaml` e do
  metainfo. O site (`https://euvinicios.github.io/Dotdeck/`) é o `website` do snap e o
  `homepage` do metainfo.

## 5. Atualizar as capturas, a ficha e o site

Sempre que a interface mudar de um jeito visível, refaça as imagens na ordem abaixo. Elas
mostram só Dots fictícios: nunca use um servidor de verdade para isso.

1. **Capturas do app.** Com um build do cliente web do OpenDots (`npm run build` gera
   `dist/client`) e a SDK do GNOME 51:

   ```bash
   make screenshots OPENDOTS_CLIENT=<OpenDots>/dist/client
   ```

   O `tools/demo_server.py` sobe sozinho numa porta livre, com Dots fictícios e o modelo
   Claude Haiku 5.5 pelo OpenRouter. Saem as capturas em inglês em `data/screenshots/` (as do
   AppStream, que o GNOME Software e a Flathub mostram) e em português, claro e escuro, em
   `data/screenshots/pt_BR/` (as do site e da loja). Abra cada uma antes de seguir.
2. **Imagens da loja e do site:**

   ```bash
   make store-assets
   ```

   Gera em `data/store/` o ícone, o banner, os cinco slides e `social-card.png` (1200×630, a
   prévia que aparece quando alguém compartilha o link do site).
3. **Lista do AppStream.** As capturas do metainfo apontam para a `main` no GitHub. Se entrar
   uma captura nova, acrescente um `<screenshot>` com a legenda em inglês e em `pt-BR` e rode
   `make validate`.
4. **Conferir o site:** `make site` monta tudo em `build/website/`. Para ver no navegador:

   ```bash
   python3 -m http.server --directory build/website 8000
   ```

5. **Publicar.** Depois do merge na `main`, o workflow **Site**
   ([`.github/workflows/pages.yml`](../.github/workflows/pages.yml)) passa a busca de
   privacidade no site e o publica no GitHub Pages. Na primeira vez, ligue o Pages em
   **Settings → Pages → Build and deployment → Source: GitHub Actions**. As imagens da loja
   são enviadas pelo painel da página da loja (seção 4), na sessão do dono, e o texto da
   ficha com `snapcraft upload-metadata` (ADR-016).

## Verificação local

```bash
sudo snap install lxd && sudo lxd init --auto
```

```bash
snapcraft pack
```

```bash
sudo snap install --dangerous ./dotdeck_*.snap && snap run dotdeck --self-test
```
