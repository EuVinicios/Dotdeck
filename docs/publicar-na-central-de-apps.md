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
| tag `v*` (ex.: `v0.1.0`) | `candidate` |

Teste o `candidate` (`sudo snap install dotdeck --candidate`) e promova para o público em
<https://snapcraft.io/dotdeck/releases> ou com:

```bash
snapcraft promote dotdeck --from-channel candidate --to-channel stable
```

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

- **Capturas de tela e banner:** gerados com `make store-assets` em `data/store/`
  (banner 1800×600 e 5 capturas 1760×1100 emolduradas com gradiente, título e subtítulo).
- **Categorias:** "Produtividade" (principal) e "Social".
- **Ícone, título, resumo, descrição, licença e links** já vêm do `snapcraft.yaml` e do
  metainfo.

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
