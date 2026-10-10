# Como contribuir

Obrigado pelo interesse no Dotdeck! Issues e pull requests são bem-vindos, em português ou
inglês.

## Ambiente

O jeito mais simples é a SDK do GNOME 51 (traz WebKitGTK 6, Libadwaita e GStreamer com WebRTC):

```bash
flatpak install --user flathub org.gnome.Sdk//51 org.flatpak.Builder
```

No Ubuntu 26.04 também dá para usar os pacotes da distribuição; a lista está no
[workflow de CI](.github/workflows/ci.yml).

## Antes de abrir o PR

- `make test` passa (unitários, WebKit, chamada de voz em loopback e bandeja).
- `make smoke` passa (a interface abre, conecta e mostra as Preferências).
- `make validate` passa (desktop file, AppStream e traduções).
- Textos novos usam `_()`; rode `make pot` e, se puder, traduza em `po/pt_BR.po`.
- Siga o estilo do código ao redor e as diretrizes de interface do GNOME (HIG).

## Traduções

As traduções ficam em [`po/`](po/). Para um idioma novo, copie `po/dotdeck.pot` para
`po/<código>.po`, traduza e acrescente o código em `po/LINGUAS`.

## Licença

Ao contribuir, você concorda em licenciar sua contribuição sob a
[GPL-3.0-or-later](LICENSE).
