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
- Mexeu no que o Dotdeck usa da página ou da API do OpenDots (seletores, rótulos e palavras em
  `src/web/`, rotas em `src/core/`)? O `tests/test_contract.py` pede o registro em
  [`src/web/contract.json`](src/web/contract.json), e
  `make contract OPENDOTS=<um checkout do OpenDots>` confere contra o projeto oficial (o
  workflow [OpenDots oficial](.github/workflows/upstream.yml) faz isso todo dia).
- Textos novos usam `_()`; rode `make pot` e, se puder, traduza em `po/pt_BR.po`.
- Mudou algo que o usuário percebe? Acrescente uma linha em `## [Não lançado]` no
  `CHANGELOG.md` (em `### Corrigido`, `### Mudado` ou `### Adicionado`) e, para a loja, um
  `<!-- loja: … -->` em inglês. Ao integrar o PR, isso vira versão e vai para a loja sozinho;
  sem linha em "Não lançado", nada é lançado
  ([como funciona](docs/publicar-na-central-de-apps.md#2b-lançamento-automático-uma-vez)).
- Siga o estilo do código ao redor e as diretrizes de interface do GNOME (HIG).

## Traduções

As traduções ficam em [`po/`](po/). Para um idioma novo, copie `po/dotdeck.pot` para
`po/<código>.po`, traduza e acrescente o código em `po/LINGUAS`.

## Licença

Ao contribuir, você concorda em licenciar sua contribuição sob a
[GPL-3.0-or-later](LICENSE).
